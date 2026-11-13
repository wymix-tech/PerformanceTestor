package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.common.page.CursorPage;
import com.perfserver.entity.TraceChain;
import com.perfserver.service.MetricQueryService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;

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

    /**
     * 调用链列表（游标分页，最新在前）。
     *
     * @param cursor 上一页返回的 {@code nextCursor}；不传即首屏
     * @param size   每页条数（默认 50，上限 500）
     * @param limit  兼容旧前端：仅在未传 {@code size} 时生效
     */
    @GetMapping("/traces")
    public ApiResponse<CursorPage<TraceChain>> traces(@RequestParam(required = false) String app,
                                                      @RequestParam(required = false) String method,
                                                      @RequestParam(required = false) String start,
                                                      @RequestParam(required = false) String end,
                                                      @RequestParam(required = false) String cursor,
                                                      @RequestParam(defaultValue = "0") int size,
                                                      @RequestParam(required = false) Integer limit) {
        LocalDateTime s = MetricQueryService.parseTime(start, LocalDateTime.now().minusHours(1));
        LocalDateTime e = MetricQueryService.parseTime(end, LocalDateTime.now());
        return ApiResponse.ok(metricQueryService.tracesPage(app, method, s, e, size, limit, cursor));
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
