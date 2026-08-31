package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.SlowCommand;
import com.perfserver.service.MetricQueryService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 中间件慢命令查询 API（Redis / ES / MQ）：按应用/类型/时间/耗时阈值过滤，按耗时倒序。
 * 需被测应用以 middlewareEnabled=true 启动 agent 才有数据。
 */
@RestController
@RequestMapping("/api")
public class SlowCommandController {

    private final MetricQueryService metricQueryService;

    public SlowCommandController(MetricQueryService metricQueryService) {
        this.metricQueryService = metricQueryService;
    }

    /** 慢命令列表（耗时倒序） */
    @GetMapping("/slow-commands")
    public ApiResponse<List<SlowCommand>> slowCommands(@RequestParam(required = false) String app,
                                                       @RequestParam(required = false) String type,
                                                       @RequestParam(required = false) String start,
                                                       @RequestParam(required = false) String end,
                                                       @RequestParam(required = false) Double minCostMs,
                                                       @RequestParam(defaultValue = "100") int limit) {
        LocalDateTime s = MetricQueryService.parseTime(start, LocalDateTime.now().minusHours(1));
        LocalDateTime e = MetricQueryService.parseTime(end, LocalDateTime.now());
        return ApiResponse.ok(metricQueryService.slowCommands(app, type, s, e, minCostMs, limit));
    }
}
