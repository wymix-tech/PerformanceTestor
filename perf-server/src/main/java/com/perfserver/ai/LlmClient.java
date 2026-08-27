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

/**
 * OpenAI 兼容大模型客户端：封装 chat/completions 调用。
 *
 * <p>无 API Key 时 {@link #available()} 返回 false，调用方降级规则模板模式，
 * 保证平台在无外部依赖时仍可演示完整流程。
 */
@Component
public class LlmClient {

    private static final Logger log = LoggerFactory.getLogger(LlmClient.class);

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
     * 发起一次对话补全。
     *
     * @param system 系统提示词
     * @param user   用户消息
     * @return 模型回复文本；调用失败或不可用时返回 null
     */
    public String chat(String system, String user) {
        if (!available()) {
            return null;
        }
        limiter.acquireUninterruptibly();
        try {
            return doChat(system, user, null);
        } finally {
            limiter.release();
        }
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
            JsonNode root = objectMapper.readTree(response.body());
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
        } catch (Exception e) {
            log.warn("llm tool call failed: {}", e.getMessage());
            return null;
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
        try {
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
            JsonNode root = objectMapper.readTree(response.body());
            return root.path("choices").path(0).path("message").path("content").asText(null);
        } catch (Exception e) {
            log.warn("llm api call failed: {}", e.getMessage());
            return null;
        }
    }
}
