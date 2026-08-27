package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.perfserver.capacity.CapacityResult;
import com.perfserver.service.CapacityService;
import org.springframework.stereotype.Component;

import java.util.Map;

/**
 * 容量评估/扩容建议工具：基于压测报告评估最大可承受并发与 QPS，并给出扩容时机与 JVM 配置建议。
 */
@Component
public class CapacityAdviceTool extends AbstractAiTool {

    private final CapacityService capacityService;

    public CapacityAdviceTool(CapacityService capacityService) {
        this.capacityService = capacityService;
    }

    @Override
    public String name() {
        return "capacity_advice";
    }

    @Override
    public String description() {
        return "容量评估与扩容建议。参数：taskId 必填（压测任务 ID）；targetP95Ms 目标 P95 阈值毫秒（可选，默认 200）；"
                + "app 应用名（可选，用于评估扩容建议）。返回目标阈值下最大可承受并发与 QPS、QPS-RT 曲线、扩容触发条件与方式、"
                + "以及 JVM 堆/GC 配置建议。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"taskId", "targetP95Ms", "app"}, new String[]{"taskId"}).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        long taskId = lng(args, "taskId", 0L);
        if (taskId <= 0) {
            return error("taskId 必填");
        }
        try {
            Double targetP95 = args.path("targetP95Ms").isMissingNode() || args.path("targetP95Ms").isNull()
                    ? null : args.path("targetP95Ms").asDouble();
            CapacityResult result = capacityService.analyzeTask(taskId, targetP95, str(args, "app", null));
            return ok(Map.of(
                    "taskId", taskId,
                    "maxConcurrency", result.getMaxConcurrency(),
                    "maxQps", result.getMaxQps(),
                    "targetP95Ms", result.getTargetP95Ms(),
                    "extrapolated", result.isExtrapolated(),
                    "curve", result.getCurve(),
                    "advices", result.getAdvices(),
                    "jvmAdvice", result.getJvmAdvice()));
        } catch (Exception e) {
            return error("容量评估失败: " + e.getMessage());
        }
    }
}
