package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.common.page.CursorPage;
import com.perfserver.entity.SlowSql;
import com.perfserver.service.MetricQueryService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;

/**
 * 慢 SQL 查询 API：按应用/时间/耗时阈值过滤，游标分页（按执行时间倒序）。
 */
@RestController
@RequestMapping("/api")
public class SlowSqlController {

    private final MetricQueryService metricQueryService;

    public SlowSqlController(MetricQueryService metricQueryService) {
        this.metricQueryService = metricQueryService;
    }

    /**
     * 慢 SQL 列表（游标分页，按执行时间倒序）。
     *
     * @param cursor 上一页返回的 {@code nextCursor}；不传即首屏
     * @param size   每页条数（默认 50，上限 500）
     * @param limit  兼容旧前端：仅在未传 {@code size} 时生效
     */
    @GetMapping("/slow-sqls")
    public ApiResponse<CursorPage<SlowSql>> slowSqls(@RequestParam(required = false) String app,
                                                     @RequestParam(required = false) String start,
                                                     @RequestParam(required = false) String end,
                                                     @RequestParam(required = false) Double minCostMs,
                                                     @RequestParam(required = false) String cursor,
                                                     @RequestParam(defaultValue = "0") int size,
                                                     @RequestParam(required = false) Integer limit) {
        LocalDateTime s = MetricQueryService.parseTime(start, LocalDateTime.now().minusHours(1));
        LocalDateTime e = MetricQueryService.parseTime(end, LocalDateTime.now());
        return ApiResponse.ok(metricQueryService.slowSqlsPage(app, s, e, minCostMs, size, limit, cursor));
    }
}
