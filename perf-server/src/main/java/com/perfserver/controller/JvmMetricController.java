package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.JvmMetric;
import com.perfserver.service.MetricQueryService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;
import java.util.List;

/**
 * JVM 指标查询 API：Web 时序图表数据源。
 */
@RestController
@RequestMapping("/api/jvm")
public class JvmMetricController {

    private final MetricQueryService metricQueryService;

    public JvmMetricController(MetricQueryService metricQueryService) {
        this.metricQueryService = metricQueryService;
    }

    /**
     * 时间窗口内 JVM 指标（时间倒序）。
     *
     * @param app   应用名（空则查询全部）
     * @param start 起始时间：epoch 毫秒或 ISO-8601（空默认最近 1 小时）
     * @param end   结束时间（空默认当前）
     * @param limit 返回条数上限（默认 500，上限 5000）
     */
    @GetMapping("/metrics")
    public ApiResponse<List<JvmMetric>> metrics(@RequestParam(required = false) String app,
                                                @RequestParam(required = false) String start,
                                                @RequestParam(required = false) String end,
                                                @RequestParam(defaultValue = "500") int limit) {
        LocalDateTime s = MetricQueryService.parseTime(start, LocalDateTime.now().minusHours(1));
        LocalDateTime e = MetricQueryService.parseTime(end, LocalDateTime.now());
        return ApiResponse.ok(metricQueryService.jvmMetrics(app, s, e, limit));
    }
}
