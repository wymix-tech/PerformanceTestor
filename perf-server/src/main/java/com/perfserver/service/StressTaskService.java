package com.perfserver.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.AppInstance;
import com.perfserver.entity.StressSample;
import com.perfserver.entity.StressTask;
import com.perfserver.mapper.AppInstanceMapper;
import com.perfserver.mapper.StressSampleMapper;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.stress.StressEngine;
import com.perfserver.stress.StressEngineRegistry;
import com.perfserver.stress.StressTaskRequest;
import com.perfserver.stress.StressTaskResult;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 压测任务服务：任务创建、参数校验、异步执行（引擎调度 + 采样落库 + 结果落库）。
 *
 * <p>压测为长耗时操作，采用独立线程池异步执行；采样点通过监听器实时写入
 * stress_sample 供前端滚动图表轮询。
 */
@Service
public class StressTaskService {

    private static final Logger log = LoggerFactory.getLogger(StressTaskService.class);

    private static final ExecutorService STRESS_EXECUTOR = Executors.newFixedThreadPool(4, r -> {
        Thread t = new Thread(r, "stress-runner");
        t.setDaemon(true);
        return t;
    });

    private final StressTaskMapper taskMapper;
    private final StressSampleMapper sampleMapper;
    private final StressEngineRegistry engineRegistry;
    private final ObjectMapper objectMapper;
    private final AppInstanceMapper appInstanceMapper;

    public StressTaskService(StressTaskMapper taskMapper,
                             StressSampleMapper sampleMapper,
                             StressEngineRegistry engineRegistry,
                             ObjectMapper objectMapper,
                             AppInstanceMapper appInstanceMapper) {
        this.taskMapper = taskMapper;
        this.sampleMapper = sampleMapper;
        this.engineRegistry = engineRegistry;
        this.objectMapper = objectMapper;
        this.appInstanceMapper = appInstanceMapper;
    }

    /** 创建任务并异步启动压测 */
    public StressTask createAndRun(StressTaskRequest request) {
        validate(request);

        StressTask task = new StressTask();
        // 场景模式无单一目标 URL：落库为可读摘要，便于列表页区分
        String fallbackName = request.isScenarioMode()
                ? "场景压测（" + request.getScenario().size() + " 步）"
                : request.getTargetUrl();
        task.setTaskName(request.getTaskName() == null || request.getTaskName().isBlank()
                ? fallbackName : request.getTaskName());
        task.setEngineType(request.getEngineType());
        task.setTargetUrl(request.isScenarioMode() ? fallbackName : request.getTargetUrl());
        task.setAppName(request.getAppName() == null || request.getAppName().isBlank()
                ? "" : request.getAppName());
        // 版本快照：发起压测时记录应用当前版本（agent 上报），供不同版本性能比对
        task.setAppVersion(resolveCurrentVersion(task.getAppName()));
        task.setMethod(request.getMethod());
        task.setConcurrency(request.getConcurrency());
        task.setDurationSec(request.getDurationSec());
        task.setQpsLimit(request.getQpsLimit());
        task.setStatus("pending");
        taskMapper.insert(task);

        final long taskId = task.getId();
        STRESS_EXECUTOR.submit(() -> runInternal(taskId, request));
        return task;
    }

    /** 查询应用当前上报版本（agent 心跳维护；查询失败/无版本返回空串） */
    private String resolveCurrentVersion(String appName) {
        if (appName == null || appName.isBlank()) {
            return "";
        }
        try {
            AppInstance inst = appInstanceMapper.findByAppName(appName);
            return inst == null || inst.getAppVersion() == null ? "" : inst.getAppVersion();
        } catch (Exception e) {
            log.warn("resolve app version failed, app={}", appName, e);
            return "";
        }
    }

    private void runInternal(long taskId, StressTaskRequest request) {
        try {
            taskMapper.updateStatus(taskId, "running");
            StressEngine engine = resolveEngine(request);
            StressTaskResult result = engine.run(request, sample -> persistSample(taskId, sample));
            result.setTaskId(taskId);
            taskMapper.updateFinished(taskId, objectMapper.writeValueAsString(result), LocalDateTime.now());
            log.info("stress task finished, id={}, total={}, qps={}", taskId, result.getTotalRequests(), result.getQps());
        } catch (Exception e) {
            log.error("stress task failed, id={}", taskId, e);
            taskMapper.updateFailed(taskId, e.getMessage());
        }
    }

    /** 采样点落库（实时图表） */
    private void persistSample(long taskId, StressTaskResult.Sample sample) {
        try {
            StressSample s = new StressSample();
            s.setTaskId(taskId);
            s.setSampleTime(Instant.ofEpochMilli(sample.getSampleTime())
                    .atZone(ZoneId.systemDefault()).toLocalDateTime());
            s.setConcurrency(sample.getConcurrency());
            s.setQps(sample.getQps());
            s.setP50Ms(sample.getP50Ms());
            s.setP95Ms(sample.getP95Ms());
            s.setP99Ms(sample.getP99Ms());
            s.setErrorCount(sample.getErrorCount());
            sampleMapper.batchInsert(List.of(s));
        } catch (Exception e) {
            log.warn("persist stress sample failed, taskId={}", taskId, e);
        }
    }

    public List<StressTask> list(String status, int limit) {
        return taskMapper.query(status, Math.max(1, Math.min(limit, 200)));
    }

    public StressTask detail(long id) {
        return taskMapper.findById(id);
    }

    public List<StressSample> samples(long taskId, int limit) {
        return sampleMapper.queryByTask(taskId, Math.max(1, Math.min(limit, 2000)));
    }

    /**
     * 历史趋势：同应用 + 同目标 URL 的已完成任务的指标序列（按完成时间正序）。
     *
     * <p>复用已有压测任务数据，不新增表：从 stress_task 取同 appName + targetUrl 的
     * 已完成任务，解析其 resultJson 提取 QPS / P95 / P99 / 错误率，供报告页画折线。
     *
     * @param appName 应用名
     * @param url     目标 URL（场景模式为「场景压测（N 步）」摘要串）
     * @param limit   条数上限
     * @return 每个元素为 {taskId, taskName, finishTime, qps, p95Ms, p99Ms, errorRate, totalRequests}
     */
    public List<Map<String, Object>> trend(String appName, String url, int limit) {
        List<Map<String, Object>> points = new ArrayList<>();
        if (appName == null || appName.isBlank() || url == null || url.isBlank()) {
            return points;
        }
        List<StressTask> tasks = taskMapper.queryByScene(appName, url, "finished", 200);
        for (StressTask t : tasks) {
            JsonNode result = parseResultJson(t);
            if (result == null) {
                continue;
            }
            Map<String, Object> point = new LinkedHashMap<>();
            point.put("taskId", t.getId());
            point.put("taskName", t.getTaskName());
            point.put("finishTime", t.getFinishTime() == null ? null : t.getFinishTime().toString());
            point.put("qps", result.path("qps").asDouble(0));
            point.put("p50Ms", result.path("p50Ms").asDouble(0));
            point.put("p95Ms", result.path("p95Ms").asDouble(0));
            point.put("p99Ms", result.path("p99Ms").asDouble(0));
            point.put("errorRate", result.path("errorRate").asDouble(0));
            point.put("totalRequests", result.path("totalRequests").asLong(0));
            points.add(point);
        }
        // 按完成时间正序：折线图从左到右即时间推进方向
        points.sort(Comparator.comparing(p -> String.valueOf(p.get("finishTime"))));
        int size = points.size();
        // 只保留最近 limit 条，仍保持正序
        if (size > limit) {
            return new ArrayList<>(points.subList(size - limit, size));
        }
        return points;
    }

    /** 解析压测结果原始 JSON（含 capacity 等追加字段） */
    public JsonNode parseResultJson(StressTask task) {
        if (task == null || task.getResultJson() == null || task.getResultJson().isBlank()) {
            return null;
        }
        try {
            return objectMapper.readTree(task.getResultJson());
        } catch (Exception e) {
            log.warn("parse stress result json failed, taskId={}", task.getId(), e);
            return null;
        }
    }

    /** 参数校验与上限保护 */
    private void validate(StressTaskRequest request) {
        validateScenario(request);
        if (request.isScenarioMode()) {
            // 场景模式：目标 URL 由各步骤自带，跳过顶层 URL 校验
            validateCommon(request);
            return;
        }
        String url = request.getTargetUrl();
        if (url == null || !(url.startsWith("http://") || url.startsWith("https://"))) {
            throw new IllegalArgumentException("压测目标 URL 必须以 http:// 或 https:// 开头");
        }
        validateCommon(request);
    }

    /** 与模式无关的公共参数校验（并发/时长/限流/预热/爬坡/引擎可用性） */
    private void validateCommon(StressTaskRequest request) {
        if (request.getConcurrency() < 1 || request.getConcurrency() > 1000) {
            throw new IllegalArgumentException("并发数必须在 1-1000 之间");
        }
        if (request.getDurationSec() < 1 || request.getDurationSec() > 3600) {
            throw new IllegalArgumentException("压测时长必须在 1-3600 秒之间");
        }
        if (request.getQpsLimit() < 0 || request.getQpsLimit() > 100_000) {
            throw new IllegalArgumentException("QPS 上限必须在 0-100000 之间");
        }
        if (request.getWarmupSec() < 0 || request.getWarmupSec() > 300) {
            throw new IllegalArgumentException("预热时长必须在 0-300 秒之间");
        }
        if (request.getRampUpSec() < 0 || request.getRampUpSec() > 600) {
            throw new IllegalArgumentException("并发爬坡时长必须在 0-600 秒之间");
        }
        // 分档校验用归一化后的结果（兼容前端的 stageConcurrencies 简写）
        List<StressTaskRequest.Stage> stages = request.resolveStages();
        if (stages != null) {
            if (stages.size() > 10) {
                throw new IllegalArgumentException("分档数量不能超过 10");
            }
            for (StressTaskRequest.Stage stage : stages) {
                if (stage.getConcurrency() < 1 || stage.getConcurrency() > 1000) {
                    throw new IllegalArgumentException("分档并发数必须在 1-1000 之间");
                }
                if (stage.getDurationSec() < 1 || stage.getDurationSec() > 600) {
                    throw new IllegalArgumentException("分档时长必须在 1-600 秒之间");
                }
            }
        }
        // 引擎可用性前置校验（如 JMeter 未安装时任务创建即拒绝，给出安装指引）
        String engineType = request.getEngineType() == null || request.getEngineType().isBlank()
                ? "http" : request.getEngineType();
        StressEngine engine = engineRegistry.get(engineType);
        if (!engine.available()) {
            throw new IllegalArgumentException("压测引擎「" + engineType + "」不可用："
                    + "JMeter 需配置 perf.stress.jmeter-home（或环境变量 JMETER_HOME）指向安装目录，"
                    + "或将 jmeter 加入 PATH；也可改用默认 http 引擎");
        }
    }

    /**
     * 多接口场景校验：步骤数上限、每步 URL 与权重合法性。
     *
     * <p>JMeter 引擎暂只支持单接口，场景任务会回退 HTTP 引擎执行（见
     * {@link #resolveEngine}）——此处不因引擎为 jmeter 而拒绝，只明确记录提示。
     */
    private void validateScenario(StressTaskRequest request) {
        if (!request.isScenarioMode()) {
            return;
        }
        List<StressTaskRequest.ScenarioStep> steps = request.getScenario();
        if (steps.size() > 20) {
            throw new IllegalArgumentException("场景步骤数量不能超过 20");
        }
        int positiveWeight = 0;
        for (int i = 0; i < steps.size(); i++) {
            StressTaskRequest.ScenarioStep step = steps.get(i);
            if (step == null) {
                throw new IllegalArgumentException("场景步骤 " + (i + 1) + " 不能为空");
            }
            if (step.getUrl() == null
                    || !(step.getUrl().startsWith("http://") || step.getUrl().startsWith("https://"))) {
                throw new IllegalArgumentException(
                        "场景步骤「" + step.getName() + "」的 URL 必须以 http:// 或 https:// 开头");
            }
            if (step.getWeight() > 0) {
                positiveWeight++;
            }
            if (step.getExtract() != null) {
                for (StressTaskRequest.VarExtract e : step.getExtract()) {
                    if (e == null || e.getVar() == null || e.getVar().isBlank()
                            || e.getPath() == null || e.getPath().isBlank()) {
                        throw new IllegalArgumentException(
                                "场景步骤「" + step.getName() + "」的变量提取规则必须同时提供 var 与 path");
                    }
                }
            }
        }
        if (positiveWeight == 0) {
            throw new IllegalArgumentException("场景至少需要 1 个权重大于 0 的步骤");
        }
    }

    /**
     * 选择实际执行引擎：JMeter 不支持多接口场景，回退 HTTP 引擎并明确提示。
     *
     * <p>回退而非拒绝：场景编排的价值在于流量模型，强行要求用户改用 http 引擎
     * 只会增加使用成本；引擎回退后任务照常执行，日志留痕便于追溯。
     */
    private StressEngine resolveEngine(StressTaskRequest request) {
        String type = request.getEngineType() == null || request.getEngineType().isBlank()
                ? "http" : request.getEngineType();
        if (request.isScenarioMode() && !"http".equals(type)) {
            log.warn("压测引擎「{}」不支持多接口场景，自动回退 http 引擎执行（步骤数 {}）",
                    type, request.getScenario().size());
            return engineRegistry.get("http");
        }
        return engineRegistry.get(type);
    }
}
