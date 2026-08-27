package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.StressSample;
import com.perfserver.entity.StressTask;
import com.perfserver.service.StressTaskService;
import com.perfserver.stress.StressTaskRequest;
import com.perfserver.stress.StressTaskResult;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 压测中心 API：启动压测任务、任务列表/详情、周期采样、压测报告。
 */
@RestController
@RequestMapping("/api/stress")
public class StressController {

    private final StressTaskService stressTaskService;

    public StressController(StressTaskService stressTaskService) {
        this.stressTaskService = stressTaskService;
    }

    /** 创建并启动压测任务（异步执行，立即返回任务） */
    @PostMapping("/tasks")
    public ApiResponse<StressTask> start(@RequestBody StressTaskRequest request) {
        return ApiResponse.ok(stressTaskService.createAndRun(request));
    }

    /** 任务列表 */
    @GetMapping("/tasks")
    public ApiResponse<List<StressTask>> tasks(@RequestParam(required = false) String status,
                                               @RequestParam(defaultValue = "50") int limit) {
        return ApiResponse.ok(stressTaskService.list(status, limit));
    }

    /** 任务详情 */
    @GetMapping("/tasks/{id}")
    public ApiResponse<StressTask> detail(@PathVariable long id) {
        return ApiResponse.ok(stressTaskService.detail(id));
    }

    /** 周期采样（实时图表） */
    @GetMapping("/tasks/{id}/samples")
    public ApiResponse<List<StressSample>> samples(@PathVariable long id,
                                                   @RequestParam(defaultValue = "500") int limit) {
        return ApiResponse.ok(stressTaskService.samples(id, limit));
    }

    /** 压测报告（含统计与分档采样点，容量评估结果由 capacity-advice 追加） */
    @GetMapping("/tasks/{id}/report")
    public ApiResponse<Map<String, Object>> report(@PathVariable long id) {
        StressTask task = stressTaskService.detail(id);
        if (task == null) {
            return ApiResponse.error("stress task not found: " + id);
        }
        Map<String, Object> data = new HashMap<>();
        data.put("task", task);
        data.put("result", stressTaskService.parseResultJson(task));
        return ApiResponse.ok(data);
    }
}
