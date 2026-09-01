package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.JvmAlertRule;
import com.perfserver.service.JvmAlertService;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

/**
 * JVM 指标阈值告警规则管理：规则增删改查、启用开关、手动检测。
 */
@RestController
@RequestMapping("/api/jvm-alert-rules")
public class JvmAlertRuleController {

    private final JvmAlertService jvmAlertService;

    public JvmAlertRuleController(JvmAlertService jvmAlertService) {
        this.jvmAlertService = jvmAlertService;
    }

    /** 规则列表（appName 为空查全部） */
    @GetMapping
    public ApiResponse<List<JvmAlertRule>> list(@RequestParam(required = false) String appName,
                                                @RequestParam(defaultValue = "100") int limit) {
        return ApiResponse.ok(jvmAlertService.list(appName, Math.min(limit, 500)));
    }

    /** 支持的指标键（前端下拉） */
    @GetMapping("/metrics")
    public ApiResponse<List<Map<String, String>>> metrics() {
        return ApiResponse.ok(JvmAlertService.metricOptions());
    }

    /** 新增规则 */
    @PostMapping
    public ApiResponse<JvmAlertRule> add(@RequestBody JvmAlertRule rule) {
        return ApiResponse.ok(jvmAlertService.add(rule));
    }

    /** 更新规则（阈值/窗口/冷却/启用/通知地址） */
    @PutMapping("/{id}")
    public ApiResponse<JvmAlertRule> update(@PathVariable long id, @RequestBody JvmAlertRule rule) {
        rule.setId(id);
        return ApiResponse.ok(jvmAlertService.update(rule));
    }

    /** 删除规则 */
    @DeleteMapping("/{id}")
    public ApiResponse<Map<String, Object>> delete(@PathVariable long id) {
        jvmAlertService.delete(id);
        return ApiResponse.ok(Map.of("deleted", id));
    }

    /** 启用/停用开关（看板直接切换） */
    @PostMapping("/{id}/enabled")
    public ApiResponse<Map<String, Object>> setEnabled(@PathVariable long id,
                                                       @RequestParam boolean enabled) {
        jvmAlertService.setEnabled(id, enabled);
        return ApiResponse.ok(Map.of("id", id, "enabled", enabled));
    }

    /** 手动触发一次判定（返回是否触发告警） */
    @PostMapping("/{id}/check")
    public ApiResponse<Map<String, Object>> check(@PathVariable long id) {
        boolean triggered = jvmAlertService.checkNow(id);
        return ApiResponse.ok(Map.of("id", id, "triggered", triggered));
    }
}
