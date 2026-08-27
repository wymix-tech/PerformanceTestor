package com.perfserver.collector;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.AppInstance;
import com.perfserver.entity.JvmMetric;
import com.perfserver.entity.SlowSql;
import com.perfserver.entity.TraceChain;
import com.perfserver.mapper.AppInstanceMapper;
import com.perfserver.mapper.JvmMetricMapper;
import com.perfserver.mapper.SlowSqlMapper;
import com.perfserver.mapper.TraceChainMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Agent 上报采集服务：接收远程探针批量上报（trace / jvm / slowSql）并落库。
 *
 * <p>上报 body 格式（与 agent 端 {@code MetricsReporter} 约定一致）：
 * {@code {"type":"trace|jvm|slowSql","appName":"...","items":[...]}}
 *
 * <p>可靠性：单批落库失败不阻断后续批次；解析异常仅记录日志，保证上报接口可用。
 */
@Service
public class AgentReportService {

    private static final Logger log = LoggerFactory.getLogger(AgentReportService.class);

    private final AppInstanceMapper appInstanceMapper;
    private final JvmMetricMapper jvmMetricMapper;
    private final TraceChainMapper traceChainMapper;
    private final SlowSqlMapper slowSqlMapper;
    private final ObjectMapper objectMapper;

    public AgentReportService(AppInstanceMapper appInstanceMapper,
                              JvmMetricMapper jvmMetricMapper,
                              TraceChainMapper traceChainMapper,
                              SlowSqlMapper slowSqlMapper,
                              ObjectMapper objectMapper) {
        this.appInstanceMapper = appInstanceMapper;
        this.jvmMetricMapper = jvmMetricMapper;
        this.traceChainMapper = traceChainMapper;
        this.slowSqlMapper = slowSqlMapper;
        this.objectMapper = objectMapper;
    }

    /**
     * 处理一次批量上报。
     *
     * @param body       上报 body
     * @param remoteHost 上报来源主机（agent 所在被测服务器，用于应用注册）
     */
    public void handleReport(Map<String, Object> body, String remoteHost) {
        String type = strVal(body.get("type"));
        String appName = strVal(body.get("appName"));
        if (appName == null || appName.isEmpty()) {
            log.warn("agent report ignored: missing appName, type={}", type);
            return;
        }
        // 心跳注册应用实例
        registerInstance(appName, remoteHost);

        Object rawItems = body.get("items");
        if (!(rawItems instanceof List<?> items) || items.isEmpty()) {
            return;
        }
        try {
            switch (type == null ? "" : type) {
                case "trace" -> saveTraces(appName, items);
                case "jvm" -> saveJvmMetrics(appName, items);
                case "slowSql" -> saveSlowSqls(appName, items);
                default -> log.warn("agent report ignored: unknown type={}, app={}", type, appName);
            }
        } catch (Exception e) {
            // 单批解析/落库失败不影响后续上报
            log.error("agent report handle failed, type={}, app={}", type, appName, e);
        }
    }

    /** 应用注册（upsert 心跳） */
    private void registerInstance(String appName, String remoteHost) {
        try {
            AppInstance instance = new AppInstance();
            instance.setAppName(appName);
            instance.setAppHost(remoteHost == null ? "" : remoteHost);
            instance.setAgentVersion("1.0.0");
            instance.setLastReportTime(LocalDateTime.now());
            appInstanceMapper.upsertByAppName(instance);
        } catch (Exception e) {
            log.warn("register app instance failed, app={}", appName, e);
        }
    }

    /** 调用链批量入库 */
    private void saveTraces(String appName, List<?> items) {
        int count = 0;
        for (Object item : items) {
            if (!(item instanceof Map<?, ?> m)) {
                continue;
            }
            try {
                @SuppressWarnings("unchecked")
                Map<String, Object> map = (Map<String, Object>) m;
                TraceChain chain = new TraceChain();
                chain.setAppName(appName);
                chain.setTraceId(strVal(map.get("traceId")));
                chain.setRootMethod(strVal(map.get("rootMethod")));
                chain.setTotalMs(doubleVal(map.get("totalMs")));
                chain.setStartTime(toLocalDateTime(longVal(map.get("startTime"))));
                Object tree = map.get("tree");
                chain.setTraceJson(tree == null ? "{}" : objectMapper.writeValueAsString(tree));
                traceChainMapper.insert(chain);
                count++;
            } catch (Exception e) {
                log.warn("save trace failed, app={}", appName, e);
            }
        }
        log.info("agent report: saved {} traces, app={}", count, appName);
    }

    /** JVM 指标批量入库 */
    private void saveJvmMetrics(String appName, List<?> items) {
        List<JvmMetric> batch = new ArrayList<>(items.size());
        for (Object item : items) {
            if (!(item instanceof Map<?, ?> m)) {
                continue;
            }
            try {
                @SuppressWarnings("unchecked")
                Map<String, Object> map = (Map<String, Object>) m;
                JvmMetric metric = new JvmMetric();
                metric.setAppName(appName);
                metric.setCollectTime(toLocalDateTime(longVal(map.get("collectTime"))));
                metric.setHeapUsedMb(doubleVal(map.get("heapUsedMb")));
                metric.setHeapMaxMb(doubleVal(map.get("heapMaxMb")));
                metric.setNonHeapUsedMb(doubleVal(map.get("nonHeapUsedMb")));
                metric.setYoungGcCount(longVal(map.get("youngGcCount")));
                metric.setYoungGcMs(longVal(map.get("youngGcMs")));
                metric.setFullGcCount(longVal(map.get("fullGcCount")));
                metric.setFullGcMs(longVal(map.get("fullGcMs")));
                metric.setThreadCount(intVal(map.get("threadCount")));
                metric.setCpuUsage(doubleVal(map.get("cpuUsage")));
                metric.setSystemCpu(doubleVal(map.get("systemCpu")));
                batch.add(metric);
            } catch (Exception e) {
                log.warn("parse jvm metric failed, app={}", appName, e);
            }
        }
        if (!batch.isEmpty()) {
            jvmMetricMapper.batchInsert(batch);
        }
        log.info("agent report: saved {} jvm metrics, app={}", batch.size(), appName);
    }

    /** 慢 SQL 批量入库 */
    private void saveSlowSqls(String appName, List<?> items) {
        List<SlowSql> batch = new ArrayList<>(items.size());
        for (Object item : items) {
            if (!(item instanceof Map<?, ?> m)) {
                continue;
            }
            try {
                @SuppressWarnings("unchecked")
                Map<String, Object> map = (Map<String, Object>) m;
                SlowSql slowSql = new SlowSql();
                slowSql.setAppName(appName);
                slowSql.setSqlText(strVal(map.get("sqlText")));
                slowSql.setCostMs(doubleVal(map.get("costMs")));
                slowSql.setSource(strVal(map.get("source")) == null ? "jdbc" : strVal(map.get("source")));
                String dbHost = strVal(map.get("dbHost"));
                slowSql.setDbHost(dbHost == null ? "" : dbHost);
                slowSql.setExecuteTime(toLocalDateTime(longVal(map.get("executeTime"))));
                batch.add(slowSql);
            } catch (Exception e) {
                log.warn("parse slow sql failed, app={}", appName, e);
            }
        }
        if (!batch.isEmpty()) {
            slowSqlMapper.batchInsert(batch);
        }
        log.info("agent report: saved {} slow sqls, app={}", batch.size(), appName);
    }

    // ===================== 类型安全取值工具 =====================

    private static String strVal(Object o) {
        return o == null ? null : String.valueOf(o);
    }

    private static long longVal(Object o) {
        return o instanceof Number n ? n.longValue() : 0L;
    }

    private static int intVal(Object o) {
        return o instanceof Number n ? n.intValue() : 0;
    }

    private static double doubleVal(Object o) {
        return o instanceof Number n ? n.doubleValue() : 0d;
    }

    /** epoch 毫秒 → LocalDateTime（平台时区） */
    private static LocalDateTime toLocalDateTime(long epochMs) {
        return Instant.ofEpochMilli(epochMs).atZone(ZoneId.systemDefault()).toLocalDateTime();
    }
}
