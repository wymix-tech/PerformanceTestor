package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.common.page.CursorPage;
import com.perfserver.entity.JvmMetric;
import com.perfserver.service.MetricQueryService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;

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
     * 时间窗口内 JVM 指标（游标分页，最新在前）。
     *
     * <p>注意排序方向：图表侧要正序画线，这里返回倒序分页数据，
     * 前端渲染图表时需自行 {@code reverse()}（一次 O(n) 内存操作，无查询代价）。
     *
     * @param app    应用名（空则查询全部）
     * @param start  起始时间：epoch 毫秒或 ISO-8601（空默认最近 1 小时）
     * @param end    结束时间（空默认当前）
     * @param cursor 上一页返回的 {@code nextCursor}；不传即首屏
     * @param size   每页条数（默认 50，上限 500）
     * @param limit  兼容旧前端：仅在未传 {@code size} 时生效
     */
    @GetMapping("/metrics")
    public ApiResponse<CursorPage<JvmMetric>> metrics(@RequestParam(required = false) String app,
                                                      @RequestParam(required = false) String start,
                                                      @RequestParam(required = false) String end,
                                                      @RequestParam(required = false) String cursor,
                                                      @RequestParam(defaultValue = "0") int size,
                                                      @RequestParam(required = false) Integer limit) {
        LocalDateTime s = MetricQueryService.parseTime(start, LocalDateTime.now().minusHours(1));
        LocalDateTime e = MetricQueryService.parseTime(end, LocalDateTime.now());
        return ApiResponse.ok(metricQueryService.jvmMetricsPage(app, s, e,
                com.perfserver.common.page.CursorPageRequest.normalizeSize(size, limit), cursor));
    }
}
