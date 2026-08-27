package com.perfserver.ai;

/**
 * AI 工具统一接口：Function Calling 中注册给大模型的工具。
 *
 * <p>实现类需提供：工具名（snake_case）、描述、OpenAI 函数参数 JSON Schema，
 * 以及执行入口（入参为模型按 Schema 生成的 JSON 字符串，返回结果 JSON 字符串）。
 */
public interface AiTool {

    /** 工具名（如 stress_test / jvm_metrics） */
    String name();

    /** 工具描述（供模型理解何时调用） */
    String description();

    /** OpenAI function.parameters 的 JSON Schema 字符串（Jackson 序列化前） */
    String parametersJson();

    /**
     * 执行工具。
     *
     * @param jsonArgs 模型生成的参数 JSON 字符串
     * @return 结果 JSON 字符串（失败时返回含 error 字段的 JSON，不抛异常）
     */
    String execute(String jsonArgs);
}
