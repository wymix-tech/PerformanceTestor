package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.perfserver.entity.StressTask;
import com.perfserver.service.StressTaskService;
import com.perfserver.stress.StressTaskRequest;
import org.springframework.stereotype.Component;

import java.util.Map;

/**
 * 压测工具：发起 HTTP 压测任务（异步执行，立即返回任务 ID 与状态）。
 */
@Component
public class StressTool extends AbstractAiTool {

    private final StressTaskService stressTaskService;

    public StressTool(StressTaskService stressTaskService) {
        this.stressTaskService = stressTaskService;
    }

    @Override
    public String name() {
        return "stress_test";
    }

    @Override
    public String description() {
        return "发起一次 HTTP 性能压测。参数：targetUrl 必填（http/https 开头）；concurrency 并发数（默认 20，1-1000）；"
                + "durationSec 持续秒数（默认 30，1-3600）；method HTTP 方法（GET/POST，默认 GET）；qpsLimit QPS 上限（0 不限）。"
                + "返回任务 ID 与状态，任务异步执行，完成后可查询结果与容量评估。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"targetUrl", "concurrency", "durationSec", "method", "qpsLimit"},
                new String[]{"targetUrl"}).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        String url = str(args, "targetUrl", "");
        if (url.isBlank()) {
            return error("targetUrl 必填");
        }
        try {
            StressTaskRequest req = new StressTaskRequest();
            req.setTargetUrl(url);
            req.setConcurrency(integer(args, "concurrency", 20));
            req.setDurationSec(integer(args, "durationSec", 30));
            req.setMethod(str(args, "method", "GET"));
            req.setQpsLimit(integer(args, "qpsLimit", 0));
            StressTask task = stressTaskService.createAndRun(req);
            return ok(Map.of(
                    "taskId", task.getId(),
                    "status", task.getStatus(),
                    "targetUrl", url,
                    "concurrency", req.getConcurrency(),
                    "durationSec", req.getDurationSec(),
                    "hint", "任务异步执行中，可用慢 SQL/容量工具或稍后查询结果"));
        } catch (Exception e) {
            return error("压测启动失败: " + e.getMessage());
        }
    }
}
