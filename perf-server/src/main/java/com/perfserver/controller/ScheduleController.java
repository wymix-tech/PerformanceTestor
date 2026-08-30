package com.perfserver.controller;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.common.ApiResponse;
import com.perfserver.entity.StressSchedule;
import com.perfserver.mapper.StressScheduleMapper;
import com.perfserver.service.BaselineCompareService;
import com.perfserver.stress.StressTaskRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

/**
 * 定时压测调度管理 API：创建/启停/删除调度，手动查看基线对比。
 */
@RestController
@RequestMapping("/api/stress/schedules")
public class ScheduleController {

    private static final Logger log = LoggerFactory.getLogger(ScheduleController.class);

    private final StressScheduleMapper scheduleMapper;
    private final BaselineCompareService baselineService;
    private final ObjectMapper objectMapper;

    public ScheduleController(StressScheduleMapper scheduleMapper,
                              BaselineCompareService baselineService,
                              ObjectMapper objectMapper) {
        this.scheduleMapper = scheduleMapper;
        this.baselineService = baselineService;
        this.objectMapper = objectMapper;
    }

    /** 创建调度：body = { scheduleName, intervalMinutes, webhookUrl?, baselineTaskId?, task: {压测参数} } */
    @PostMapping
    public ApiResponse<StressSchedule> create(@RequestBody Map<String, Object> body) {
        try {
            String name = str(body.get("scheduleName"));
            if (name == null || name.isBlank()) {
                return ApiResponse.error(400, "调度名称不能为空");
            }
            int interval = (int) longVal(body.getOrDefault("intervalMinutes", 60));
            if (interval < 1 || interval > 10080) {
                return ApiResponse.error(400, "执行间隔必须在 1-10080 分钟之间");
            }
            Object taskObj = body.get("task");
            if (!(taskObj instanceof Map<?, ?>)) {
                return ApiResponse.error(400, "缺少压测任务模板（task 字段）");
            }
            // 校验任务模板可解析（不执行）
            StressTaskRequest request = objectMapper.convertValue(taskObj, StressTaskRequest.class);
            if (request.getTargetUrl() == null || request.getTargetUrl().isBlank()) {
                return ApiResponse.error(400, "压测任务模板缺少 targetUrl");
            }

            StressSchedule s = new StressSchedule();
            s.setScheduleName(name);
            s.setTaskJson(objectMapper.writeValueAsString(request));
            s.setIntervalMinutes(interval);
            s.setEnabled(1);
            s.setBaselineTaskId(longVal(body.getOrDefault("baselineTaskId", 0)));
            s.setWebhookUrl(str(body.getOrDefault("webhookUrl", "")));
            s.setNextRunTime(LocalDateTime.now().plusMinutes(interval));
            scheduleMapper.insert(s);
            return ApiResponse.ok(s);
        } catch (Exception e) {
            log.warn("create schedule failed", e);
            return ApiResponse.error(400, "创建调度失败: " + e.getMessage());
        }
    }

    /** 调度列表 */
    @GetMapping
    public ApiResponse<List<StressSchedule>> list() {
        return ApiResponse.ok(scheduleMapper.listAll());
    }

    /** 启停 */
    @PutMapping("/{id}/enabled")
    public ApiResponse<Void> toggle(@PathVariable long id, @RequestParam boolean enabled) {
        StressSchedule s = scheduleMapper.findById(id);
        if (s == null) {
            return ApiResponse.error(404, "调度不存在");
        }
        scheduleMapper.updateEnabled(id, enabled ? 1 : 0);
        if (enabled) {
            // 重启时推进 next_run_time（避免停用期积压立即触发风暴）
            scheduleMapper.updateRunState(id, 0,
                    s.getNextRunTime(), LocalDateTime.now().plusMinutes(Math.max(1, s.getIntervalMinutes())));
        }
        return ApiResponse.ok(null);
    }

    @DeleteMapping("/{id}")
    public ApiResponse<Void> delete(@PathVariable long id) {
        scheduleMapper.deleteById(id);
        return ApiResponse.ok(null);
    }

    /** 基线对比（手动查看：不传 taskId 时取该调度上次触发任务） */
    @GetMapping("/{id}/diff")
    public ApiResponse<BaselineCompareService.BaselineDiff> diff(@PathVariable long id,
                                                                 @RequestParam(required = false) Long taskId) {
        StressSchedule s = scheduleMapper.findById(id);
        if (s == null) {
            return ApiResponse.error(404, "调度不存在");
        }
        long target = taskId != null && taskId > 0 ? taskId
                : (s.getLastTaskId() != null ? s.getLastTaskId() : 0);
        if (target <= 0) {
            return ApiResponse.error(400, "该调度尚未触发过压测任务");
        }
        long baseline = s.getBaselineTaskId() != null ? s.getBaselineTaskId() : 0;
        return ApiResponse.ok(baselineService.diff(target, baseline));
    }

    private static String str(Object o) {
        return o == null ? null : String.valueOf(o);
    }

    private static long longVal(Object o) {
        return o instanceof Number n ? n.longValue() : 0L;
    }
}
