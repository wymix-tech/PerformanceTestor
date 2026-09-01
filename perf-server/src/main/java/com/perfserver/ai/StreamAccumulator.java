package com.perfserver.ai;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.ai.LlmClient.ChatResult;
import com.perfserver.ai.LlmClient.ToolCall;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * OpenAI 兼容流式响应累加器：把逐 chunk 到达的 delta 合并成一次完整的 {@link ChatResult}。
 *
 * <p>协议要点（{@code stream=true}）：
 * <ul>
 *   <li>每行 {@code data: <json>}，以 {@code data: [DONE]} 结束；</li>
 *   <li>正文增量在 {@code choices[0].delta.content}；</li>
 *   <li>工具调用按 {@code choices[0].delta.tool_calls[]} 增量下发：{@code index} 标识第几个调用，
 *       {@code id}/{@code function.name} 只在首块出现，{@code function.arguments} 逐块拼接。</li>
 * </ul>
 *
 * <p>本类不依赖网络，便于单测覆盖解析与合并逻辑。
 */
public class StreamAccumulator {

    private final StringBuilder content = new StringBuilder();
    private final Map<Integer, ToolCall> calls = new LinkedHashMap<>();
    private final Map<Integer, StringBuilder> argBuffers = new HashMap<>();

    /**
     * 消费一条 {@code data:} 载荷。
     *
     * @param dataJson {@code data:} 前缀后的 JSON 文本（不含 {@code [DONE]}）
     * @param mapper   JSON 解析器
     * @return 本 chunk 新增的正文 token（0 或 1 个），供调用方推流；非法 JSON 静默忽略
     */
    public List<String> consume(String dataJson, ObjectMapper mapper) {
        List<String> tokens = new ArrayList<>(1);
        if (dataJson == null || dataJson.isBlank()) {
            return tokens;
        }
        JsonNode root;
        try {
            root = mapper.readTree(dataJson);
        } catch (Exception e) {
            return tokens;
        }
        if (root == null || !root.isObject()) {
            return tokens;
        }
        JsonNode delta = resolveDelta(root);
        if (delta.isMissingNode() || delta.isNull()) {
            return tokens;
        }
        String text = delta.path("content").asText(null);
        if (text != null && !text.isEmpty()) {
            content.append(text);
            tokens.add(text);
        }
        mergeToolCalls(delta.path("tool_calls"));
        return tokens;
    }

    /** 合并为一次完整的模型回复 */
    public ChatResult toResult() {
        ChatResult result = new ChatResult();
        result.setContent(content.length() == 0 ? null : content.toString());
        List<ToolCall> merged = new ArrayList<>(calls.size());
        for (Map.Entry<Integer, ToolCall> e : calls.entrySet()) {
            ToolCall call = e.getValue();
            StringBuilder buf = argBuffers.get(e.getKey());
            String args = buf == null || buf.length() == 0 ? "{}" : buf.toString();
            call.setArguments(args);
            merged.add(call);
        }
        result.setToolCalls(merged);
        return result;
    }

    /** 已累积的正文（供取消/异常时取部分结果） */
    public String partialContent() {
        return content.toString();
    }

    /** 兼容不同网关：优先 choices[0].delta，其次顶层 delta */
    private static JsonNode resolveDelta(JsonNode root) {
        JsonNode choices = root.path("choices");
        if (choices.isArray() && choices.size() > 0) {
            return choices.get(0).path("delta");
        }
        return root.path("delta");
    }

    private void mergeToolCalls(JsonNode toolCalls) {
        if (toolCalls == null || !toolCalls.isArray()) {
            return;
        }
        for (JsonNode tc : toolCalls) {
            int index = tc.path("index").asInt(0);
            ToolCall call = calls.computeIfAbsent(index, k -> new ToolCall());
            String id = tc.path("id").asText(null);
            if (id != null && !id.isEmpty()) {
                call.setId(id);
            }
            JsonNode fn = tc.path("function");
            if (!fn.isMissingNode() && !fn.isNull()) {
                String name = fn.path("name").asText(null);
                if (name != null && !name.isEmpty()) {
                    call.setName(name);
                }
                String args = fn.path("arguments").asText(null);
                if (args != null && !args.isEmpty()) {
                    argBuffers.computeIfAbsent(index, k -> new StringBuilder()).append(args);
                }
            }
        }
    }
}
