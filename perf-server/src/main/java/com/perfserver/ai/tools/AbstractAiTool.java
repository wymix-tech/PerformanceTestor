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
        ObjectNode props = mapper.createObjectNode();
        for (String p : properties) {
            props.putObject(p);
        }
        return buildParams(props, required);
    }

    /**
     * 构建带类型描述的 function.parameters。
     *
     * <p>与 {@link #buildParams(String[], String[])} 的区别：properties 由调用方自行
     * 构造（可带 {@code type} / {@code description} / 嵌套 {@code items}），用于描述
     * 数组、对象这类复杂参数——LLM 只有拿到形状描述才会产出结构正确的 JSON。
     *
     * @param properties 已构造好的 properties 节点
     * @param required   必填参数名（可为 null/空）
     */
    protected ObjectNode buildParams(ObjectNode properties, String[] required) {
        ObjectNode schema = mapper.createObjectNode();
        schema.put("type", "object");
        schema.set("properties", properties);
        if (required != null && required.length > 0) {
            var arr = schema.putArray("required");
            for (String r : required) {
                arr.add(r);
            }
        }
        return schema;
    }

    /** 构造一个带描述的标量属性（type 取 string / integer / number / boolean） */
    protected ObjectNode prop(String type, String description) {
        ObjectNode node = mapper.createObjectNode();
        node.put("type", type);
        node.put("description", description);
        return node;
    }

    /** 构造数组属性（元素为对象时传 items，否则传 null） */
    protected ObjectNode arrayProp(String description, ObjectNode items) {
        ObjectNode node = mapper.createObjectNode();
        node.put("type", "array");
        node.put("description", description);
        if (items != null) {
            node.set("items", items);
        }
        return node;
    }

    /** 读取数组参数（缺失或非数组返回空数组） */
    protected JsonNode arr(JsonNode args, String key) {
        JsonNode v = args.path(key);
        return v.isArray() ? v : mapper.createArrayNode();
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
