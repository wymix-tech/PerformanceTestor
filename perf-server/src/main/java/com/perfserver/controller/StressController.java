package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.auth.JwtAuthInterceptor;
import com.perfserver.entity.StressSample;
import com.perfserver.entity.StressTask;
import com.perfserver.entity.StressTemplate;
import com.perfserver.service.StressGateService;
import com.perfserver.service.StressTaskService;
import com.perfserver.service.StressTemplateService;
import jakarta.servlet.http.HttpServletRequest;
import com.perfserver.stress.StressTaskRequest;
import com.perfserver.stress.StressTaskResult;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.DeleteMapping;
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
    private final StressGateService stressGateService;
    private final StressTemplateService stressTemplateService;
    private final HttpServletRequest request;

    public StressController(StressTaskService stressTaskService, StressGateService stressGateService,
                            StressTemplateService stressTemplateService, HttpServletRequest request) {
        this.stressTaskService = stressTaskService;
        this.stressGateService = stressGateService;
        this.stressTemplateService = stressTemplateService;
        this.request = request;
    }

    /** 创建并启动压测任务（异步执行，立即返回任务） */
    @PostMapping("/tasks")
    public ApiResponse<StressTask> start(@RequestBody StressTaskRequest request) {
        return ApiResponse.ok(stressTaskService.createAndRun(request));
    }

    /**
     * 性能回归门禁（CI 用）：创建压测 → 等待完成 → 与基线比对 → 返回可判定结论。
     *
     * <p>同步阻塞到压测完成（受 timeoutSec 限制），流水线一次调用即可拿到
     * {@code data.passed} 作为构建判定依据（失败时 HTTP 仍为 200，
     * 便于脚本用 JSON 解析出明细摘要，退出码由脚本按 passed 决定）。
     */
    @PostMapping("/gate")
    public ApiResponse<StressGateService.GateVerdict> gate(
            @RequestBody StressTaskRequest request,
            @RequestParam(required = false) Long baselineTaskId,
            @RequestParam(required = false) Long timeoutSec,
            @RequestParam(defaultValue = "false") boolean failOnNoBaseline,
            @RequestParam(required = false) String webhook) {
        StressGateService.GateVerdict verdict = stressGateService.runGate(
                request, baselineTaskId == null ? 0 : baselineTaskId,
                timeoutSec == null ? 0 : timeoutSec, failOnNoBaseline, webhook);
        return ApiResponse.ok(verdict);
    }

    /**
     * 历史趋势：同应用 + 同 URL 的最近完成任务的核心指标序列（按完成时间正序）。
     *
     * @param appName 应用名（必填，用于隔离不同应用）
     * @param url     目标 URL（必填，场景模式用「场景压测（N 步）」摘要串）
     * @param limit   返回条数（默认 20，上限 100）
     */
    @GetMapping("/trend")
    public ApiResponse<List<Map<String, Object>>> trend(@RequestParam String appName,
                                                        @RequestParam String url,
                                                        @RequestParam(defaultValue = "20") int limit) {
        return ApiResponse.ok(stressTaskService.trend(appName, url, Math.min(limit, 100)));
    }

    /**
     * 压测模板列表（P2-3）。
     *
     * @param appName 归属应用（空 = 全部；指定应用时同时返回通用模板 {@code app_name=''})
     */
    @GetMapping("/templates")
    public ApiResponse<List<StressTemplate>> templates(@RequestParam(required = false) String appName,
                                                       @RequestParam(defaultValue = "100") int limit) {
        return ApiResponse.ok(stressTemplateService.list(appName, limit));
    }

    /** 模板详情（「从模板填充」用） */
    @GetMapping("/templates/{id}")
    public ApiResponse<StressTemplate> templateDetail(@PathVariable long id) {
        return ApiResponse.ok(stressTemplateService.detail(id));
    }

    /** 保存为模板（把当前压测表单参数沉淀下来复用） */
    @PostMapping("/templates")
    public ApiResponse<StressTemplate> saveTemplate(@RequestBody StressTemplate template) {
        return ApiResponse.ok(stressTemplateService.create(template, currentOperator()));
    }

    /** 更新模板 */
    @PutMapping("/templates/{id}")
    public ApiResponse<StressTemplate> updateTemplate(@PathVariable long id, @RequestBody StressTemplate template) {
        return ApiResponse.ok(stressTemplateService.update(id, template));
    }

    /** 删除模板 */
    @DeleteMapping("/templates/{id}")
    public ApiResponse<Void> deleteTemplate(@PathVariable long id) {
        stressTemplateService.delete(id);
        return ApiResponse.ok();
    }

    /** 当前操作人（模板创建人展示用；与 SourceController 同口径） */
    private String currentOperator() {
        if (Boolean.TRUE.equals(request.getAttribute(JwtAuthInterceptor.CI_ATTR))) {
            return "ci";
        }
        Object operator = request.getAttribute(JwtAuthInterceptor.USER_ATTR);
        return operator == null ? "unknown" : String.valueOf(operator);
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
