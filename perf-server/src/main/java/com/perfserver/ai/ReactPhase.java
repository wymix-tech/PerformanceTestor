package com.perfserver.ai;

/**
 * ReAct 循环阶段：Thought（推理）→ Action（调用工具）→ Observation（回填观察）→ Finish（结论）。
 */
public enum ReactPhase {

    /** 推理：模型输出 Thought 并决定下一步动作 */
    THINK("think"),
    /** 行动：执行模型请求的工具 */
    ACT("act"),
    /** 观察：把工具返回（截断后）回填上下文 */
    OBSERVE("observe"),
    /** 结束：模型给出最终结论，不再请求工具 */
    FINISH("finish");

    private final String code;

    ReactPhase(String code) {
        this.code = code;
    }

    /** 前端/日志用阶段码 */
    public String code() {
        return code;
    }
}
