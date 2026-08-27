package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.perfserver.entity.JvmMetric;
import com.perfserver.service.MetricQueryService;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * JVM 指标查询工具：返回最近时间窗口内的堆/GC/线程/CPU 指标汇总与采样点。
 */
@Component
public class JvmMetricsTool extends AbstractAiTool {

    private final MetricQueryService metricQueryService;

    public JvmMetricsTool(MetricQueryService metricQueryService) {
        this.metricQueryService = metricQueryService;
    }

    @Override
    public String name() {
        return "jvm_metrics";
    }

    @Override
    public String description() {
        return "查询被测应用最近一段时间的 JVM 指标（堆内存、GC、线程、CPU）。参数：app 应用名（可选，缺省查询全部）；"
                + "minutes 查询最近多少分钟（默认 60）；limit 返回采样点数量上限（默认 100）。"
                + "返回最新指标、均值与时间序列采样点。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"app", "minutes", "limit"}, null).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        String app = str(args, "app", null);
        int minutes = integer(args, "minutes", 60);
        int limit = integer(args, "limit", 100);
        try {
            LocalDateTime end = LocalDateTime.now();
            List<JvmMetric> metrics = metricQueryService.jvmMetrics(app, end.minusMinutes(minutes), end, limit);
            if (metrics.isEmpty()) {
                return ok(Map.of("app", app == null ? "all" : app, "count", 0, "message", "窗口内无 JVM 指标，请确认应用已挂载 agent"));
            }
            JvmMetric latest = metrics.get(metrics.size() - 1);
            Map<String, Object> summary = new LinkedHashMap<>();
            summary.put("heapUsedMb", latest.getHeapUsedMb());
            summary.put("heapMaxMb", latest.getHeapMaxMb());
            summary.put("nonHeapUsedMb", latest.getNonHeapUsedMb());
            summary.put("youngGcCount", latest.getYoungGcCount());
            summary.put("fullGcCount", latest.getFullGcCount());
            summary.put("threadCount", latest.getThreadCount());
            summary.put("cpuUsage", latest.getCpuUsage());

            ArrayNode points = mapper.createArrayNode();
            for (JvmMetric m : metrics) {
                var p = points.addObject();
                p.put("time", m.getCollectTime() == null ? "" : m.getCollectTime().toString());
                p.put("heapUsedMb", m.getHeapUsedMb());
                p.put("fullGcCount", m.getFullGcCount());
                p.put("cpuUsage", m.getCpuUsage());
            }
            return ok(Map.of("app", app == null ? "all" : app, "count", metrics.size(),
                    "latest", summary, "points", points));
        } catch (Exception e) {
            return error("JVM 指标查询失败: " + e.getMessage());
        }
    }
}
