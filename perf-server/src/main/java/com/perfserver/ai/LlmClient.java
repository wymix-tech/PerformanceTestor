package com.perfserver.ai;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.Semaphore;
import java.util.function.Consumer;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * OpenAI 兼容大模型客户端：封装 chat/completions 调用。
 *
 * <p>无 API Key 时 {@link #available()} 返回 false，调用方降级规则模板模式，
 * 保证平台在无外部依赖时仍可演示完整流程。
 */
@Component
public class LlmClient {

    private static final Logger log = LoggerFactory.getLogger(LlmClient.class);

    /** 错误响应体最多回读的行数（够排障即可，避免大响应体占内存） */
    private static final int MAX_ERROR_LINES = 20;

    private final ObjectMapper objectMapper = new ObjectMapper();
    private final HttpClient httpClient;
    private final Semaphore limiter;

    @Value("${perf.ai.enabled:true}")
    private boolean enabled;
    @Value("${perf.ai.base-url:https://api.openai.com/v1}")
    private String baseUrl;
    @Value("${perf.ai.api-key:}")
    private String apiKey;
    @Value("${perf.ai.model:gpt-4o-mini}")
    private String model;
    @Value("${perf.ai.timeout-sec:60}")
    private int timeoutSec;
    @Value("${perf.ai.stream-timeout-sec:300}")
    private int streamTimeoutSec;

    public LlmClient(@Value("${perf.ai.max-concurrent-requests:4}") int maxConcurrentRequests) {
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();
        this.limiter = new Semaphore(Math.max(1, maxConcurrentRequests));
    }

    /** 是否可用（启用且配置了 API Key） */
    public boolean available() {
        return enabled && apiKey != null && !apiKey.isBlank();
    }

    /**
     * 带 JSON Schema 约束的对话补全（强制模型输出合法 JSON）。
     *
     * @return 模型输出的原始 JSON 字符串；失败或不可用返回 null
     */
    public String chatJson(String system, String user, JsonNode schema) {
        if (!available()) {
            return null;
        }
        limiter.acquireUninterruptibly();
        try {
            return doChat(system, user, schema);
        } finally {
            limiter.release();
        }
    }

    /**
     * Function Calling 对话补全：支持多轮消息（system/user/assistant/tool）与工具调用。
     *
     * @param messages 完整消息数组（OpenAI 格式 ObjectNode 列表）
     * @param tools    工具函数定义数组（OpenAI format）
     * @return 模型回复（含最终 content 与/或 tool_calls）；失败或不可用时返回 null
     */
    public ChatResult chatWithTools(List<JsonNode> messages, ArrayNode tools) {
        return chatWithTools(messages, tools, null);
    }

    /**
     * Function Calling 对话补全（可选流式）。
     *
     * @param messages  完整消息数组（OpenAI 格式 ObjectNode 列表）
     * @param tools     工具函数定义数组（OpenAI format）
     * @param tokenSink 正文增量消费器；非 null 时启用 {@code stream=true} 逐 token 回调，
     *                  null 时走一次性响应（兼容旧路径）
     * @return 模型回复（含最终 content 与/或 tool_calls）；失败或不可用时返回 null
     */
    public ChatResult chatWithTools(List<JsonNode> messages, ArrayNode tools, Consumer<String> tokenSink) {
        if (!available()) {
            return null;
        }
        limiter.acquireUninterruptibly();
        try {
            ObjectNode body = objectMapper.createObjectNode();
            body.put("model", model);
            body.put("temperature", 0.3);
            ArrayNode msgArray = body.putArray("messages");
            messages.forEach(msgArray::add);
            body.set("tools", tools);
            body.put("tool_choice", "auto");

            if (tokenSink == null) {
                JsonNode root = postChat(body);
                return root == null ? null : parseChatResult(root);
            }
            body.put("stream", true);
            return postChatStream(body, tokenSink);
        } finally {
            limiter.release();
        }
    }

    /** 解析一次性响应的 choices[0].message */
    private ChatResult parseChatResult(JsonNode root) {
        JsonNode message = root.path("choices").path(0).path("message");
        ChatResult result = new ChatResult();
        result.setContent(message.path("content").asText(null));
        JsonNode calls = message.path("tool_calls");
        if (calls.isArray()) {
            for (JsonNode call : calls) {
                ToolCall tc = new ToolCall();
                tc.setId(call.path("id").asText());
                tc.setName(call.path("function").path("name").asText());
                tc.setArguments(call.path("function").path("arguments").asText("{}"));
                result.getToolCalls().add(tc);
            }
        }
        return result;
    }

    /**
     * 流式对话：逐行读取 SSE，正文增量经 tokenSink 实时回调，工具调用增量累加后一次性返回。
     *
     * <p>降级：连接中断/读流异常时，若已累积到正文则返回部分结果（token 已推给前端，
     * 丢弃会与前端已渲染内容不一致）；无任何内容时返回 null，由调用方降级规则模式。
     *
     * @return 合并后的完整回复；无可用内容时返回 null（调用方降级规则模式）
     */
    private ChatResult postChatStream(ObjectNode body, Consumer<String> tokenSink) {
        StreamAccumulator accumulator = new StreamAccumulator();
        try {
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl.replaceAll("/+$", "") + "/chat/completions"))
                    .timeout(Duration.ofSeconds(Math.max(timeoutSec, streamTimeoutSec)))
                    .header("Content-Type", "application/json")
                    .header("Authorization", "Bearer " + apiKey)
                    .header("Accept", "text/event-stream")
                    .POST(HttpRequest.BodyPublishers.ofString(objectMapper.writeValueAsString(body)))
                    .build();

            HttpResponse<Stream<String>> response =
                    httpClient.send(request, HttpResponse.BodyHandlers.ofLines());
            if (response.statusCode() != 200) {
                // 必须消费错误响应体：JDK HttpClient 只在响应体读完后才归还连接，
                // 未读的 body 会让底层连接被丢弃而非复用，ReAct 每轮一次调用会放大为频繁重建 TLS
                log.warn("llm stream api error, status={}, body={}", response.statusCode(),
                        errorBody(response.body()));
                return null;
            }
            try (Stream<String> lines = response.body()) {
                for (String line : (Iterable<String>) lines::iterator) {
                    if (line == null) {
                        continue;
                    }
                    String trimmed = line.trim();
                    // SSE 帧：忽略 event:/comment(:) 行，只处理 data: 行
                    if (trimmed.isEmpty() || !trimmed.startsWith("data:")) {
                        continue;
                    }
                    String data = trimmed.substring("data:".length()).trim();
                    if ("[DONE]".equals(data)) {
                        break;
                    }
                    for (String token : accumulator.consume(data, objectMapper)) {
                        tokenSink.accept(token);
                    }
                }
            }
            return accumulator.toResult();
        } catch (Exception e) {
            log.warn("llm stream api call failed: {}", e.getMessage());
            // 已推给前端的 token 不能丢弃：有部分正文时降级返回，保持与前端已渲染内容一致
            String partial = accumulator.partialContent();
            if (partial != null && !partial.isBlank()) {
                ChatResult partialResult = new ChatResult();
                partialResult.setContent(partial);
                return partialResult;
            }
            return null;
        }
    }

    /**
     * 读取错误响应体前若干行：既让连接得以归还连接池，也保留服务端给出的具体报错。
     *
     * @return 错误正文摘要；读取失败返回空串（排障信息缺失不应影响主流程）
     */
    private static String errorBody(Stream<String> body) {
        if (body == null) {
            return "";
        }
        try (Stream<String> lines = body) {
            return lines.limit(MAX_ERROR_LINES).collect(Collectors.joining("\n"));
        } catch (Exception e) {
            return "";
        }
    }

    /** 工具调用结果封装 */
    public static class ToolCall {
        private String id;
        private String name;
        private String arguments;

        public String getId() { return id; }
        public void setId(String id) { this.id = id; }
        public String getName() { return name; }
        public void setName(String name) { this.name = name; }
        public String getArguments() { return arguments; }
        public void setArguments(String arguments) { this.arguments = arguments; }
    }

    /** 对话结果：最终回复文本 + 模型请求的工具调用列表 */
    public static class ChatResult {
        private String content;
        private List<ToolCall> toolCalls = new java.util.ArrayList<>();

        public String getContent() { return content; }
        public void setContent(String content) { this.content = content; }
        public List<ToolCall> getToolCalls() { return toolCalls; }
        public void setToolCalls(List<ToolCall> toolCalls) { this.toolCalls = toolCalls; }
    }

    private String doChat(String system, String user, JsonNode responseSchema) {
        ObjectNode body = objectMapper.createObjectNode();
        body.put("model", model);
        body.put("temperature", 0.3);
        ArrayNode messages = body.putArray("messages");
        ObjectNode sysMsg = messages.addObject();
        sysMsg.put("role", "system");
        sysMsg.put("content", system);
        ObjectNode userMsg = messages.addObject();
        userMsg.put("role", "user");
        userMsg.put("content", user);
        if (responseSchema != null) {
            ObjectNode resp = body.putObject("response_format");
            resp.put("type", "json_schema");
            resp.set("json_schema", responseSchema);
        }

        JsonNode root = postChat(body);
        return root == null ? null
                : root.path("choices").path(0).path("message").path("content").asText(null);
    }

    /**
     * 发送 chat/completions 请求（调用方需已获取并发许可）。
     *
     * @return 响应 JSON 根节点；请求失败或非 200 时返回 null
     */
    private JsonNode postChat(ObjectNode body) {
        try {
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl.replaceAll("/+$", "") + "/chat/completions"))
                    .timeout(Duration.ofSeconds(timeoutSec))
                    .header("Content-Type", "application/json")
                    .header("Authorization", "Bearer " + apiKey)
                    .POST(HttpRequest.BodyPublishers.ofString(objectMapper.writeValueAsString(body)))
                    .build();

            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) {
                log.warn("llm api error, status={}, body={}", response.statusCode(),
                        response.body() == null ? "" : response.body().substring(0, Math.min(500, response.body().length())));
                return null;
            }
            return objectMapper.readTree(response.body());
        } catch (Exception e) {
            log.warn("llm api call failed: {}", e.getMessage());
            return null;
        }
    }
}
