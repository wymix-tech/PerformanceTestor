package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.perfserver.entity.TraceChain;
import com.perfserver.service.MetricQueryService;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

/**
 * 调用链查询工具：按应用/根方法关键字返回最近调用链列表（含总耗时与根方法）。
 */
@Component
public class TraceTool extends AbstractAiTool {

    private final MetricQueryService metricQueryService;

    public TraceTool(MetricQueryService metricQueryService) {
        this.metricQueryService = metricQueryService;
    }

    @Override
    public String name() {
        return "trace";
    }

    @Override
    public String description() {
        return "查询业务方法调用链。参数：app 应用名（可选）；method 根方法关键字（类名或方法名，可选）；"
                + "minutes 最近多少分钟（默认 60）；limit 条数上限（默认 50）。返回调用链 ID、根方法、总耗时与开始时间，"
                + "可按 traceId 进一步关联源码分析热点方法。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"app", "method", "minutes", "limit"}, null).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        String app = str(args, "app", null);
        String method = str(args, "method", null);
        int minutes = integer(args, "minutes", 60);
        int limit = integer(args, "limit", 50);
        try {
            LocalDateTime end = LocalDateTime.now();
            List<TraceChain> traces = metricQueryService.traces(app, method, end.minusMinutes(minutes), end, limit);
            ArrayNode list = mapper.createArrayNode();
            for (TraceChain t : traces) {
                var n = list.addObject();
                n.put("traceId", t.getTraceId());
                n.put("appName", t.getAppName());
                n.put("rootMethod", t.getRootMethod());
                n.put("totalMs", t.getTotalMs());
                n.put("startTime", t.getStartTime() == null ? "" : t.getStartTime().toString());
            }
            return ok(Map.of("count", traces.size(), "traces", list));
        } catch (Exception e) {
            return error("调用链查询失败: " + e.getMessage());
        }
    }
}
