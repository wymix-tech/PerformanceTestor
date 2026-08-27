package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.AppInstance;
import com.perfserver.service.MetricQueryService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 应用实例查询 API：供前端应用切换下拉与总览页使用。
 */
@RestController
@RequestMapping("/api")
public class AppController {

    private final MetricQueryService metricQueryService;

    public AppController(MetricQueryService metricQueryService) {
        this.metricQueryService = metricQueryService;
    }

    /** 应用实例列表（在线优先） */
    @GetMapping("/apps")
    public ApiResponse<List<AppInstance>> apps() {
        return ApiResponse.ok(metricQueryService.listApps());
    }
}
