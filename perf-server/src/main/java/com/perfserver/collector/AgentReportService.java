package com.perfserver.collector;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.AppInstance;
import com.perfserver.entity.DbConn;
import com.perfserver.entity.JvmMetric;
import com.perfserver.entity.SlowSql;
import com.perfserver.entity.TraceChain;
import com.perfserver.mapper.AppInstanceMapper;
import com.perfserver.mapper.DbConnMapper;
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
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

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

    /** 项目是否存在（供 Controller 预检，处理在 handleReport 内还有双保险） */
    public boolean projectExists(String project) {
        return projectRegistry.exists(project);
    }

    /** 未知项目告警限频：项目名 → 上次告警时间（同类每 60s 最多一条） */
    private final Map<String, Long> unknownProjectWarnAt = new java.util.concurrent.ConcurrentHashMap<>();
    private static final long WARN_INTERVAL_MS = 60_000;

    /** 限频告警：未知项目上报被丢弃 */
    private void warnUnknownProjectOnce(String project, String type) {
        long now = System.currentTimeMillis();
        Long last = unknownProjectWarnAt.get(project);
        if (last == null || now - last >= WARN_INTERVAL_MS) {
            unknownProjectWarnAt.put(project, now);
            log.warn("agent report DROPPED: 项目「{}」不存在于源码仓库（targetProjectName 与平台项目名不一致？）, type={}", project, type);
        }
    }

    private final AppInstanceMapper appInstanceMapper;
    private final JvmMetricMapper jvmMetricMapper;
    private final TraceChainMapper traceChainMapper;
    private final SlowSqlMapper slowSqlMapper;
    private final DbConnMapper dbConnMapper;
    private final ProjectRegistry projectRegistry;
    private final ObjectMapper objectMapper;

    public AgentReportService(AppInstanceMapper appInstanceMapper,
                              JvmMetricMapper jvmMetricMapper,
                              TraceChainMapper traceChainMapper,
                              SlowSqlMapper slowSqlMapper,
                              DbConnMapper dbConnMapper,
                              ProjectRegistry projectRegistry,
                              ObjectMapper objectMapper) {
        this.appInstanceMapper = appInstanceMapper;
        this.jvmMetricMapper = jvmMetricMapper;
        this.traceChainMapper = traceChainMapper;
        this.slowSqlMapper = slowSqlMapper;
        this.dbConnMapper = dbConnMapper;
        this.projectRegistry = projectRegistry;
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
        // ===== 项目准入校验（数据守门）：项目不存在 → 全部丢弃 =====
        // targetProjectName 未随报文携带时回退 appName（旧版 agent 兼容）
        String targetProject = strVal(body.get("targetProjectName"));
        if (targetProject == null || targetProject.isBlank()) {
            targetProject = appName;
        }
        if (!projectRegistry.exists(targetProject)) {
            // 限频告警：避免未知项目高频上报刷屏（同类告警每分钟最多一条）
            warnUnknownProjectOnce(targetProject, type);
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
        // 本批已处理过的数据库地址，避免同一批重复自动注册
        Set<String> autoRegged = new HashSet<>();
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
                // agent 上报了库地址且该应用尚未关联连接 → 自动注册（仅注册一次）
                if (dbHost != null && !dbHost.isBlank() && autoRegged.add(dbHost)) {
                    ensureAutoDbConn(appName, dbHost);
                }
                // 触发方法链（JSON 数组，根 → 叶），供源码精确定位与界面展示
                Object chain = map.get("methodChain");
                slowSql.setMethodChain(chain == null ? null : objectMapper.writeValueAsString(chain));
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

    /**
     * 自动注册数据库连接：agent 上报慢 SQL 携带库地址（dbHost）时，
     * 若该应用尚未关联任何连接（appName 绑定或 host 匹配），自动插入一条，
     * 便于后续 EXPLAIN 分析直接命中；已关联过则不重复插入。
     * 凭据（用户名/密码）agent 不上报，留空由用户在界面补充。
     */
    private void ensureAutoDbConn(String appName, String dbHost) {
        if (dbHost == null || dbHost.isBlank()) {
            return;
        }
        try {
            List<DbConn> exists = dbConnMapper.findByAppOrHost(appName, dbHost);
            for (DbConn c : exists) {
                boolean appBound = appName.equals(c.getAppName());
                boolean hostMatch = c.getHost() != null && !c.getHost().isBlank()
                        && (dbHost.contains(c.getHost()) || c.getHost().contains(dbHost));
                if (appBound || hostMatch) {
                    // 已有关联，不重复自动插入
                    return;
                }
            }
            DbConn conn = new DbConn();
            conn.setConnName("自动关联-" + appName);
            conn.setHost(dbHost);
            conn.setAppName(appName);
            conn.setDbUrl("jdbc:mysql://" + dbHost
                    + "?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai&rewriteBatchedStatements=true");
            conn.setUsername("");   // agent 不携带凭据，由用户后补
            conn.setPassword("");
            conn.setIsDefault(0);
            dbConnMapper.insert(conn);
            log.info("agent report: 自动注册数据库连接, app={}, host={}", appName, dbHost);
        } catch (Exception e) {
            log.warn("auto register db conn failed, app={}, host={}", appName, dbHost, e);
        }
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
