package com.perfserver.ai;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.stereotype.Component;

import java.util.List;

/**
 * 系统提示词与 OpenAI Function Calling 工具清单构建。
 *
 * <p>系统提示词按 {@link AiRole} 分角色组装：公共基座（能力清单 + 推理格式 + 使用原则）
 * 叠加角色专属段落，由编排层 {@link AiAgentService} 按用户意图 {@link #detectRole(String)} 路由。
 */
@Component
public class PromptBuilder {

    /** 公共基座：角色定位、能力清单、ReAct 推理格式、使用原则、远程部署能力 */
    private static final String BASE_PROMPT = """
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
            10. 检索知识库（search_knowledge）：检索平台沉淀的历史分析结论与修复经验（中文即可）

            ## 推理格式（ReAct）
            每一轮严格按 Thought → Action → Observation 推进：
            - Thought：一句话说明本轮要做什么、为什么（直接输出自然语言，不要输出 JSON）
            - Action：调用一个或多个工具获取证据
            - Observation：由系统把工具返回回填给你，不要伪造观察结果
            重复该循环直到证据充分，再给出最终结论。禁止在无工具证据的情况下断言性能结论。

            ## 使用原则
            - 始终使用中文回答，语言简洁专业，结论先行。
            - 用户问"慢 SQL 根因/优化建议"时：先 search_knowledge 检索历史结论，再 slow_sql 查询，
              然后 analyze_code_issues 分析源码根因并生成方案，若用户选定某方案，再用 generate_fix_prompt 输出修复提示词。
            - 用户问"能承受多少并发/何时扩容"时：先确认或发起压测（stress_test），再调 capacity_advice。
            - 工具返回 error 时如实说明失败原因，并给出用户可操作的下一步建议。
            - 严禁编造数据：一切结论基于工具返回结果；工具返回空时说明"暂无数据"。
            - 知识库无命中（search_knowledge 返回空）只代表没有可复用的历史结论，
              不代表当前没有问题，也不代表可以跳过证据采集：按证据链独立分析，
              不要拿无关的历史结论硬套。
            - search_knowledge 返回项的 appName 为空表示"全局经验"（跨项目通用判读），
              非空表示"本应用历史结论"，两者都可用，但引用时需说明来源以免口径混淆。
            - 发起压测/灌数据前确认参数；并发、时长取合理默认值即可，不必反复询问。
            - 工具返回会被截断（约 2000 字），如需更多细节请缩小查询范围后再次调用。

            ## 远程部署能力（可选前置步骤）
            工具 build_project / deploy_and_start / remote_app_status 可将平台编译的被测应用部署到目标服务器：
            - 用户要求"部署并压测 / 重新启动被测应用"时：先 build_project（repoId 必填），
              再 deploy_and_start（connId 为用户指定的 SSH 连接配置；jarPath 用 build_project 返回值；
              agentPath 不填则自动探测 perf-agent/target/perf-agent.jar 以 -javaagent 挂载），
              启动后用 remote_app_status 确认进程与健康检查，再进入压测流程。
            - 用户未提供 connId 时列出可用的 SSH 连接（提及连接名称）请用户选择，不要猜测。
            - SSH/部署失败（网络不通、认证失败）时如实报告，不要重试超过一次。
            """;

    /** 角色：压测执行（灌数 → 压测 → 观测 → 报告闭环） */
    private static final String STRESS_EXECUTOR_PROMPT = """

            ## 当前角色：压测执行
            你的目标是拿到可信的性能数据，并把它组织成可执行的结论。
            - 优先保证数据可信：先确认被测服务可达、数据已就绪，再发起压测。
            - 输出顺序：场景与参数 → 核心指标（QPS / P95 / P99 / 错误率）→ 容量结论 → 下一步建议。

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

    /** 角色：根因分析（证据驱动归因） */
    private static final String ROOT_CAUSE_ANALYST_PROMPT = """

            ## 当前角色：根因分析
            你的目标是解释"为什么慢"，而不是着急给方案。
            - 证据优先：先 search_knowledge 检索历史结论，再用 slow_sql / trace / jvm_metrics / stress_test
              结果补齐证据链；证据不足时先补采集，不要凭经验下结论。
            - 区分相关与因果：多个可疑点时，给出各自贡献度排序与验证方法。
            - 结论结构固定为四段：
              【现象】可观测的指标异常（含具体数值与时间范围）
              【证据】支撑结论的工具返回摘要（SQL/方法/指标）
              【根因】直接原因 + 深层原因（设计/配置/数据分布）
              【影响】影响范围与严重级别，以及建议的下一步验证动作
            """;

    /** 角色：修复建议（可落地改造方案） */
    private static final String FIX_ADVISOR_PROMPT = """

            ## 当前角色：修复建议
            你的目标是给出能直接落地的改造方案。
            - 先 search_knowledge 检索同类问题的既有结论，避免与历史方案冲突；已有结论则优先复用并补充差异。
            - 每个方案固定包含：涉及文件、修改点、预期收益（尽量给量化预估）、风险与回滚方式。
            - 给出 2-3 个方案时按"改动成本从低到高"排序，并明确推荐项与理由。
            - 用户确认方案后再调用 generate_fix_prompt 输出结构化修复提示词，不要未经确认直接生成。
            """;

    private final ObjectMapper mapper = new ObjectMapper();
    private final List<AiTool> tools;

    public PromptBuilder(List<AiTool> tools) {
        this.tools = tools;
    }

    /** 系统提示词（无意图时的通用角色） */
    public String buildSystemPrompt() {
        return roleFor(AiRole.GENERAL);
    }

    /**
     * 按角色组装系统提示词：公共基座 + 角色专属段落。
     */
    public String roleFor(AiRole role) {
        if (role == null) {
            return BASE_PROMPT;
        }
        return switch (role) {
            case STRESS_EXECUTOR -> BASE_PROMPT + STRESS_EXECUTOR_PROMPT;
            case ROOT_CAUSE_ANALYST -> BASE_PROMPT + ROOT_CAUSE_ANALYST_PROMPT;
            case FIX_ADVISOR -> BASE_PROMPT + FIX_ADVISOR_PROMPT;
            case GENERAL -> BASE_PROMPT;
        };
    }

    /**
     * 意图路由：按用户消息关键词判定本轮使用的角色。
     *
     * <p>判定顺序即优先级：显式闭环指令 → 强修复意图 → 归因意图 → 压测意图 → 弱修复意图 → 观测类问题。
     */
    public AiRole detectRole(String message) {
        String m = message == null ? "" : message.toLowerCase();
        if (m.isBlank()) {
            return AiRole.GENERAL;
        }
        // 1. 显式闭环/一键指令：整体是压测执行流程
        if (containsAny(m, "自动执行", "一键检测", "一键", "完整闭环", "闭环", "快速开始")) {
            return AiRole.STRESS_EXECUTOR;
        }
        // 2. 强修复意图：要提示词/改法
        if (containsAny(m, "提示词", "怎么改", "如何改", "如何优化", "整改", "fix", "patch")) {
            return AiRole.FIX_ADVISOR;
        }
        // 3. 归因意图
        if (containsAny(m, "根因", "为什么", "原因", "瓶颈", "诊断", "排查", "分析")) {
            return AiRole.ROOT_CAUSE_ANALYST;
        }
        // 4. 压测意图
        if (containsAny(m, "压测", "并发", "容量", "扩容", "灌数", "灌注", "灌入", "造数", "qps", "吞吐", "基准", "报告")) {
            return AiRole.STRESS_EXECUTOR;
        }
        // 5. 弱修复意图
        if (containsAny(m, "修复", "方案", "优化")) {
            return AiRole.FIX_ADVISOR;
        }
        // 6. 观测类问题描述：默认按根因分析处理
        if (containsAny(m, "慢", "调用链", "热点", "gc", "内存", "线程", "超时")) {
            return AiRole.ROOT_CAUSE_ANALYST;
        }
        return AiRole.GENERAL;
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

    private static boolean containsAny(String text, String... keywords) {
        for (String k : keywords) {
            if (text.contains(k)) {
                return true;
            }
        }
        return false;
    }
}
