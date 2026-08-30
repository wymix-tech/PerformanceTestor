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
import java.util.List;
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
        task.setTaskName(request.getTaskName() == null || request.getTaskName().isBlank()
                ? request.getTargetUrl() : request.getTaskName());
        task.setEngineType(request.getEngineType());
        task.setTargetUrl(request.getTargetUrl());
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
            StressEngine engine = engineRegistry.get(request.getEngineType());
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
        String url = request.getTargetUrl();
        if (url == null || !(url.startsWith("http://") || url.startsWith("https://"))) {
            throw new IllegalArgumentException("压测目标 URL 必须以 http:// 或 https:// 开头");
        }
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
        // 引擎可用性前置校验（如 JMeter 未安装时任务创建即拒绝，给出安装指引）
        String engineType = request.getEngineType() == null || request.getEngineType().isBlank()
                ? "http" : request.getEngineType();
        StressEngine engine = engineRegistry.get(engineType);
        if (!engine.available()) {
            throw new IllegalArgumentException("压测引擎「" + engineType + "」不可用："
                    + "JMeter 需配置 perf.stress.jmeter-home（或环境变量 JMETER_HOME）指向安装目录，"
                    + "或将 jmeter 加入 PATH；也可改用默认 http 引擎");
        }
        if (request.getStages() != null) {
            if (request.getStages().size() > 10) {
                throw new IllegalArgumentException("分档数量不能超过 10");
            }
            for (StressTaskRequest.Stage stage : request.getStages()) {
                if (stage.getConcurrency() < 1 || stage.getConcurrency() > 1000) {
                    throw new IllegalArgumentException("分档并发数必须在 1-1000 之间");
                }
                if (stage.getDurationSec() < 1 || stage.getDurationSec() > 600) {
                    throw new IllegalArgumentException("分档时长必须在 1-600 秒之间");
                }
            }
        }
    }
}
