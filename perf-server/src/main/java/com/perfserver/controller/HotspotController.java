package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.HotspotMethod;
import com.perfserver.mapper.HotspotMethodMapper;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 方法级 CPU 热点查询 API：时间窗内按命中次数聚合降序（火焰排行）。
 */
@RestController
@RequestMapping("/api")
public class HotspotController {

    private final HotspotMethodMapper hotspotMethodMapper;

    public HotspotController(HotspotMethodMapper hotspotMethodMapper) {
        this.hotspotMethodMapper = hotspotMethodMapper;
    }

    /** CPU 热点方法排行（默认最近 1 小时 top 30） */
    @GetMapping("/hotspots")
    public ApiResponse<List<HotspotMethod>> hotspots(@RequestParam(required = false) String app,
                                                     @RequestParam(required = false) String start,
                                                     @RequestParam(required = false) String end,
                                                     @RequestParam(defaultValue = "30") int limit) {
        LocalDateTime s = parseTime(start, LocalDateTime.now().minusHours(1));
        LocalDateTime e = parseTime(end, LocalDateTime.now());
        int bounded = Math.max(1, Math.min(limit, 200));
        return ApiResponse.ok(hotspotMethodMapper.queryTop(app, s, e, bounded));
    }

    /** 时间参数解析：ISO 格式或 yyyy-MM-dd HH:mm:ss（与 MetricQueryService.parseTime 对齐） */
    private static LocalDateTime parseTime(String raw, LocalDateTime fallback) {
        if (raw == null || raw.isBlank()) {
            return fallback;
        }
        try {
            return LocalDateTime.parse(raw);
        } catch (Exception ignored) {
            // fallthrough
        }
        try {
            return LocalDateTime.parse(raw, java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"));
        } catch (Exception ignored) {
            return fallback;
        }
    }
}
