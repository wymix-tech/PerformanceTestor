package com.perfserver.ai;

/**
 * AI 编排角色：按用户意图路由不同的 system prompt 与工具使用策略。
 */
public enum AiRole {

    /** 通用助手：未命中明确意图时使用（保留全部能力说明） */
    GENERAL("通用助手"),
    /** 压测执行：灌数/压测/观测/报告闭环执行 */
    STRESS_EXECUTOR("压测执行"),
    /** 根因分析：证据驱动的归因与瓶颈定位 */
    ROOT_CAUSE_ANALYST("根因分析"),
    /** 修复建议：给出可落地的改造方案与修复提示词 */
    FIX_ADVISOR("修复建议");

    private final String label;

    AiRole(String label) {
        this.label = label;
    }

    /** 中文角色名（前端/日志展示） */
    public String label() {
        return label;
    }
}
