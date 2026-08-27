package com.perfserver.controller;

import com.perfserver.capacity.CapacityResult;
import com.perfserver.common.ApiResponse;
import com.perfserver.service.CapacityService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * 容量评估与扩容建议 API。
 */
@RestController
@RequestMapping("/api/capacity")
public class CapacityController {

    private final CapacityService capacityService;

    public CapacityController(CapacityService capacityService) {
        this.capacityService = capacityService;
    }

    /**
     * 对指定压测任务执行容量评估（幂等，结果落库到任务 result_json）。
     * body: {taskId, targetP95Ms?, app?}
     */
    @PostMapping("/analyze")
    public ApiResponse<CapacityResult> analyze(@RequestBody Map<String, Object> body) {
        long taskId = ((Number) body.get("taskId")).longValue();
        Double targetP95Ms = body.get("targetP95Ms") == null ? null
                : ((Number) body.get("targetP95Ms")).doubleValue();
        String app = body.get("app") == null ? null : String.valueOf(body.get("app"));
        return ApiResponse.ok(capacityService.analyzeTask(taskId, targetP95Ms, app));
    }

    /** 查询已落库的容量评估结果 */
    @GetMapping("/task/{taskId}")
    public ApiResponse<CapacityResult> stored(@PathVariable long taskId) {
        CapacityResult result = capacityService.getStoredCapacity(taskId);
        return result == null ? ApiResponse.ok(null) : ApiResponse.ok(result);
    }

    /** 基于近期 JVM 指标的实时扩容与 JVM 配置建议 */
    @GetMapping("/advice")
    public ApiResponse<Map<String, Object>> advice(@RequestParam(required = false) String app) {
        return ApiResponse.ok(capacityService.realtimeAdvice(app));
    }
}
