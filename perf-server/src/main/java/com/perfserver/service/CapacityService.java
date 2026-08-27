package com.perfserver.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.capacity.CapacityAnalyzer;
import com.perfserver.capacity.CapacityProperties;
import com.perfserver.capacity.CapacityResult;
import com.perfserver.capacity.JvmConfigAdvisor;
import com.perfserver.capacity.ScaleAdviceEngine;
import com.perfserver.entity.JvmMetric;
import com.perfserver.entity.StressTask;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.stress.StressTaskResult;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 容量评估编排服务：对已完成的压测任务执行容量分析，结果追加到任务 result_json
 * 的 capacity 字段落库（幂等，可重复执行）。同时提供基于近期 JVM 指标的实时建议。
 */
@Service
public class CapacityService {

    private static final Logger log = LoggerFactory.getLogger(CapacityService.class);

    private final StressTaskMapper taskMapper;
    private final MetricQueryService metricQueryService;
    private final CapacityAnalyzer capacityAnalyzer;
    private final ScaleAdviceEngine scaleAdviceEngine;
    private final JvmConfigAdvisor jvmConfigAdvisor;
    private final CapacityProperties props;
    private final com.fasterxml.jackson.databind.ObjectMapper objectMapper;

    public CapacityService(StressTaskMapper taskMapper,
                           MetricQueryService metricQueryService,
                           CapacityAnalyzer capacityAnalyzer,
                           ScaleAdviceEngine scaleAdviceEngine,
                           JvmConfigAdvisor jvmConfigAdvisor,
                           CapacityProperties props,
                           com.fasterxml.jackson.databind.ObjectMapper objectMapper) {
        this.taskMapper = taskMapper;
        this.metricQueryService = metricQueryService;
        this.capacityAnalyzer = capacityAnalyzer;
        this.scaleAdviceEngine = scaleAdviceEngine;
        this.jvmConfigAdvisor = jvmConfigAdvisor;
        this.props = props;
        this.objectMapper = objectMapper;
    }

    /**
     * 对压测任务执行容量评估并落库。
     *
     * @param taskId      压测任务 ID
     * @param targetP95Ms 目标 P95 阈值（空用配置默认）
     * @param app         被测应用名（用于 JVM 指标分析，可空）
     */
    public CapacityResult analyzeTask(long taskId, Double targetP95Ms, String app) {
        StressTask task = taskMapper.findById(taskId);
        if (task == null) {
            throw new IllegalArgumentException("压测任务不存在: " + taskId);
        }
        if (task.getResultJson() == null || task.getResultJson().isBlank()) {
            throw new IllegalArgumentException("压测任务尚未完成，无法评估容量: " + taskId);
        }
        try {
            StressTaskResult result = objectMapper.readValue(task.getResultJson(), StressTaskResult.class);
            double target = targetP95Ms == null || targetP95Ms <= 0 ? props.getTargetP95Ms() : targetP95Ms;

            CapacityResult capacity = capacityAnalyzer.analyze(result.getStagePoints(), target);

            // 近期 JVM 指标（用于规则引擎与 JVM 建议）
            List<JvmMetric> metrics = loadRecentMetrics(app);

            capacity.setAdvices(scaleAdviceEngine.evaluate(capacity, metrics, result));
            capacity.setJvmAdvice(jvmConfigAdvisor.advise(metrics));

            // 落库：result_json.capacity
            JsonNode root = objectMapper.readTree(task.getResultJson());
            if (root instanceof ObjectNode node) {
                node.putPOJO("capacity", capacity);
                taskMapper.updateResultJson(taskId, objectMapper.writeValueAsString(node));
            }
            log.info("capacity analyzed, taskId={}, maxConcurrency={}, maxQps={}",
                    taskId, capacity.getMaxConcurrency(), capacity.getMaxQps());
            return capacity;
        } catch (Exception e) {
            throw new IllegalArgumentException("容量评估失败: " + e.getMessage(), e);
        }
    }

    /** 读取已落库的容量评估结果（未评估返回 null） */
    public CapacityResult getStoredCapacity(long taskId) {
        StressTask task = taskMapper.findById(taskId);
        if (task == null || task.getResultJson() == null || task.getResultJson().isBlank()) {
            return null;
        }
        try {
            JsonNode root = objectMapper.readTree(task.getResultJson());
            JsonNode capacity = root.path("capacity");
            return capacity.isMissingNode() || capacity.isNull() ? null
                    : objectMapper.treeToValue(capacity, CapacityResult.class);
        } catch (Exception e) {
            log.warn("read stored capacity failed, taskId={}", taskId, e);
            return null;
        }
    }

    /** 基于近期 JVM 指标给实时建议（无压测任务场景） */
    public Map<String, Object> realtimeAdvice(String app) {
        List<JvmMetric> metrics = loadRecentMetrics(app);
        CapacityResult capacity = new CapacityResult();
        capacity.setTargetP95Ms(props.getTargetP95Ms());
        capacity.setAdvices(scaleAdviceEngine.evaluate(capacity, metrics, null));
        capacity.setJvmAdvice(jvmConfigAdvisor.advise(metrics));

        Map<String, Object> data = new HashMap<>();
        data.put("capacity", capacity);
        data.put("metrics", metrics);
        return data;
    }

    private List<JvmMetric> loadRecentMetrics(String app) {
        if (app == null || app.isBlank()) {
            return List.of();
        }
        LocalDateTime end = LocalDateTime.now();
        LocalDateTime start = end.minusMinutes(props.getSampleWindowMin());
        return metricQueryService.jvmMetrics(app, start, end, 500);
    }
}
