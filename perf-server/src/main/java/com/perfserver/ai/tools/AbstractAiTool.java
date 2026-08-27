package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.ai.AiTool;

/**
 * 工具基类：封装参数 Schema 构建、入参解析与统一返回格式（ok/error）。
 *
 * <p>所有工具执行失败均返回 {@code {"error": "..."}} JSON，不向编排层抛异常，
 * 保证 Function Calling 循环稳定推进。
 */
public abstract class AbstractAiTool implements AiTool {

    protected final ObjectMapper mapper = new ObjectMapper();

    /** 构建 OpenAI function.parameters（object + properties + required） */
    protected ObjectNode buildParams(String[] properties, String[] required) {
        ObjectNode schema = mapper.createObjectNode();
        schema.put("type", "object");
        ObjectNode props = schema.putObject("properties");
        for (String p : properties) {
            props.putObject(p);
        }
        if (required != null && required.length > 0) {
            var arr = schema.putArray("required");
            for (String r : required) {
                arr.add(r);
            }
        }
        return schema;
    }

    /** 解析模型参数 JSON，解析失败返回空对象（字段缺省兜底） */
    protected JsonNode parse(String jsonArgs) {
        try {
            JsonNode node = mapper.readTree(jsonArgs == null ? "{}" : jsonArgs);
            return node == null || !node.isObject() ? mapper.createObjectNode() : node;
        } catch (Exception e) {
            return mapper.createObjectNode();
        }
    }

    protected String error(String message) {
        ObjectNode node = mapper.createObjectNode();
        node.put("error", message);
        return node.toString();
    }

    protected String ok(Object data) {
        ObjectNode node = mapper.createObjectNode();
        node.set("data", mapper.valueToTree(data));
        return node.toString();
    }

    protected String str(JsonNode args, String key, String defaultValue) {
        JsonNode v = args.path(key);
        return v.isMissingNode() || v.isNull() ? defaultValue : v.asText(defaultValue);
    }

    protected int integer(JsonNode args, String key, int defaultValue) {
        JsonNode v = args.path(key);
        return v.isMissingNode() || v.isNull() ? defaultValue : v.asInt(defaultValue);
    }

    protected long lng(JsonNode args, String key, long defaultValue) {
        JsonNode v = args.path(key);
        return v.isMissingNode() || v.isNull() ? defaultValue : v.asLong(defaultValue);
    }
}
