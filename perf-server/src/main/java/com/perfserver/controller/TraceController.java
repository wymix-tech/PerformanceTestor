package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.TraceChain;
import com.perfserver.service.MetricQueryService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 调用链查询 API：调用链列表 + 详情（调用树 JSON 供前端渲染火焰图）。
 */
@RestController
@RequestMapping("/api")
public class TraceController {

    private final MetricQueryService metricQueryService;

    public TraceController(MetricQueryService metricQueryService) {
        this.metricQueryService = metricQueryService;
    }

    /** 调用链列表（时间倒序） */
    @GetMapping("/traces")
    public ApiResponse<List<TraceChain>> traces(@RequestParam(required = false) String app,
                                                @RequestParam(required = false) String method,
                                                @RequestParam(required = false) String start,
                                                @RequestParam(required = false) String end,
                                                @RequestParam(defaultValue = "100") int limit) {
        LocalDateTime s = MetricQueryService.parseTime(start, LocalDateTime.now().minusHours(1));
        LocalDateTime e = MetricQueryService.parseTime(end, LocalDateTime.now());
        return ApiResponse.ok(metricQueryService.traces(app, method, s, e, limit));
    }

    /** 调用链详情（含 trace_json 调用树） */
    @GetMapping("/traces/{traceId}")
    public ApiResponse<TraceChain> detail(@PathVariable String traceId) {
        TraceChain chain = metricQueryService.traceDetail(traceId);
        if (chain == null) {
            return ApiResponse.error("trace not found: " + traceId);
        }
        return ApiResponse.ok(chain);
    }
}
