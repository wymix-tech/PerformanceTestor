package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.SlowSql;
import com.perfserver.service.MetricQueryService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 慢 SQL 查询 API：按应用/时间/耗时阈值过滤，按耗时倒序。
 */
@RestController
@RequestMapping("/api")
public class SlowSqlController {

    private final MetricQueryService metricQueryService;

    public SlowSqlController(MetricQueryService metricQueryService) {
        this.metricQueryService = metricQueryService;
    }

    /** 慢 SQL 列表（耗时倒序） */
    @GetMapping("/slow-sqls")
    public ApiResponse<List<SlowSql>> slowSqls(@RequestParam(required = false) String app,
                                               @RequestParam(required = false) String start,
                                               @RequestParam(required = false) String end,
                                               @RequestParam(required = false) Double minCostMs,
                                               @RequestParam(defaultValue = "100") int limit) {
        LocalDateTime s = MetricQueryService.parseTime(start, LocalDateTime.now().minusHours(1));
        LocalDateTime e = MetricQueryService.parseTime(end, LocalDateTime.now());
        return ApiResponse.ok(metricQueryService.slowSqls(app, s, e, minCostMs, limit));
    }
}
