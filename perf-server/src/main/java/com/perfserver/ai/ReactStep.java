package com.perfserver.ai;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * ReAct 单步记录：一轮 Thought/Action/Observation 的结构化留痕，供前端展示推理过程与单测断言。
 *
 * @param phase       所处阶段
 * @param thought     模型本轮推理文本（可能为自动补全的占位描述）
 * @param action      调用的工具名（FINISH 阶段为 null）
 * @param actionInput 工具入参 JSON（FINISH 阶段为 null）
 * @param observation 工具返回回填内容（已截断，FINISH 阶段为 null）
 */
public record ReactStep(ReactPhase phase, String thought, String action, String actionInput, String observation) {

    /** 转为前端可序列化的 Map（允许 null 字段，故不用 Map.of） */
    public Map<String, Object> toMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("phase", phase.code());
        m.put("thought", thought);
        m.put("action", action);
        m.put("actionInput", actionInput);
        m.put("observation", observation);
        return m;
    }
}
