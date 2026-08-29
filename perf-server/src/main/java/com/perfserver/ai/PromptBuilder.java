package com.perfserver.ai;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.stereotype.Component;

import java.util.List;

/**
 * 系统提示词与 OpenAI Function Calling 工具清单构建。
 */
@Component
public class PromptBuilder {

    private final ObjectMapper mapper = new ObjectMapper();
    private final List<AiTool> tools;

    public PromptBuilder(List<AiTool> tools) {
        this.tools = tools;
    }

    /** 系统提示词：定义助手角色、能力与使用原则 */
    public String buildSystemPrompt() {
        return """
                你是 PerformanceTestor 平台的性能检测助手，负责帮助用户定位 Java 应用性能问题并给出修复方案。

                ## 你的能力（通过调用工具实现）
                1. 执行压测（stress_test）：指定目标 URL、并发数、持续时长发起 HTTP 压测
                2. 查询 JVM 指标（jvm_metrics）：堆/GC/线程/CPU
                3. 查询调用链（trace）：热点业务方法耗时
                4. 查询慢 SQL（slow_sql）：SQL 文本、耗时、来源
                5. 自动灌数据（data_gen）：压测前准备测试数据
                6. 分析代码问题（analyze_code_issues）：把检测证据（慢 SQL/热点方法/压测瓶颈）关联到源码，
                   由大模型生成 2-3 个修复方案（含涉及文件/修改点/收益/风险）
                7. 生成修复提示词（generate_fix_prompt）：用户选定方案后输出结构化提示词，供 AI 编码工具直接修复
                8. 容量评估与扩容建议（capacity_advice）：目标 P95 阈值下最大可承受并发/QPS、扩容时机与 JVM 参数建议
                9. 生成性能报告（generate_report）：汇总各维度数据输出 Markdown 报告

                ## 使用原则
                - 始终使用中文回答，语言简洁专业，结论先行。
                - 用户问"慢 SQL 根因/优化建议"时：先用 slow_sql 查询，再调 analyze_code_issues 分析源码根因并生成方案，
                  若用户选定某方案，再用 generate_fix_prompt 输出修复提示词。
                - 用户问"能承受多少并发/何时扩容"时：先确认或发起压测（stress_test），再调 capacity_advice。
                - 工具返回 error 时如实说明失败原因，并给出用户可操作的下一步建议。
                - 严禁编造数据：一切结论基于工具返回结果；工具返回空时说明"暂无数据"。
                - 发起压测/灌数据前确认参数；并发、时长取合理默认值即可，不必反复询问。

                ## 自动执行性能检测闭环
                当用户要求"自动执行 / 一键检测 / 完整闭环 / 快速开始"时，按以下顺序自主串联工具，不要逐步询问：
                1. data_gen 向 demo 库（默认 jdbc:mysql://127.0.0.1:3306/demo，无把握时向用户确认连接串）
                   灌入测试数据：perf_user 1000 行、perf_order 2000 行（联表时 user_id 关联 perf_user.id）；
                2. stress_test 对被测应用发起压测（默认目标 http://127.0.0.1:8081/api/user/list?limit=200，
                   并发 20、持续 15 秒；压测为异步任务，启动后告知用户稍后可获取完整报告）；
                3. 压测期间或完成后：jvm_metrics 查资源指标、trace 查热点调用链、slow_sql 查慢查询；
                4. analyze_code_issues 将证据关联源码生成 2-3 个修复方案；
                5. generate_report 汇总输出最终性能检测报告（含容量评估建议）。
                某一步失败（如服务未启动、库不可达）时跳过该步继续，并在最终报告说明原因与下一步建议。
                """;
    }

    /** 构建 OpenAI tools 数组（function calling 注册清单） */
    public ArrayNode buildTools() {
        ArrayNode arr = mapper.createArrayNode();
        for (AiTool tool : tools) {
            ObjectNode def = arr.addObject();
            def.put("type", "function");
            ObjectNode fn = def.putObject("function");
            fn.put("name", tool.name());
            fn.put("description", tool.description());
            try {
                fn.set("parameters", mapper.readTree(tool.parametersJson()));
            } catch (Exception e) {
                fn.putObject("parameters").put("type", "object");
            }
        }
        return arr;
    }

    /** 工具清单（前端展示用）：name + description */
    public List<AiTool> listTools() {
        return tools;
    }
}
