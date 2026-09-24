package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.common.page.CursorPage;
import com.perfserver.entity.SlowCommand;
import com.perfserver.service.MetricQueryService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;

/**
 * 中间件慢命令查询 API（Redis / ES / MQ）：按应用/类型/时间/耗时阈值过滤，游标分页（按执行时间倒序）。
 * 需被测应用以 middlewareEnabled=true 启动 agent 才有数据。
 */
@RestController
@RequestMapping("/api")
public class SlowCommandController {

    private final MetricQueryService metricQueryService;

    public SlowCommandController(MetricQueryService metricQueryService) {
        this.metricQueryService = metricQueryService;
    }

    /**
     * 慢命令列表（游标分页，按执行时间倒序）。
     *
     * @param cursor 上一页返回的 {@code nextCursor}；不传即首屏
     * @param size   每页条数（默认 50，上限 500）
     * @param limit  兼容旧前端：仅在未传 {@code size} 时生效
     */
    @GetMapping("/slow-commands")
    public ApiResponse<CursorPage<SlowCommand>> slowCommands(@RequestParam(required = false) String app,
                                                             @RequestParam(required = false) String type,
                                                             @RequestParam(required = false) String start,
                                                             @RequestParam(required = false) String end,
                                                             @RequestParam(required = false) Double minCostMs,
                                                             @RequestParam(required = false) String cursor,
                                                             @RequestParam(defaultValue = "0") int size,
                                                             @RequestParam(required = false) Integer limit) {
        LocalDateTime s = MetricQueryService.parseTime(start, LocalDateTime.now().minusHours(1));
        LocalDateTime e = MetricQueryService.parseTime(end, LocalDateTime.now());
        return ApiResponse.ok(metricQueryService.slowCommandsPage(app, type, s, e, minCostMs, size, limit, cursor));
    }
}
