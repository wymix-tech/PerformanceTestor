package com.perfagent.reporter;

import com.perfagent.config.AgentConfig;
import com.perfagent.trace.TraceNode;
import com.perfagent.util.JsonUtil;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.jar.Attributes;
import java.util.jar.JarFile;
import java.util.jar.Manifest;

/**
 * 指标上报器：本地队列缓冲 + 单线程定时 flush + 失败指数退避重试 + 超时丢弃。
 *
 * <p>设计目标（分离部署可靠性）：agent 与被测应用在同一进程，
 * 上报走异步线程，业务线程零阻塞；平台不可达时数据在本地缓冲，
 * 达到上限丢弃最旧数据，网络抖动不影响被测应用。
 *
 * <p>上报 body 格式：{"type":"trace|jvm|slowSql","appName":"...","items":[...]}
 */
public class MetricsReporter {

    /** 上报类型常量（与平台 AgentReportService 约定一致） */
    public static final String TYPE_TRACE = "trace";
    public static final String TYPE_JVM = "jvm";
    public static final String TYPE_SLOW_SQL = "slowSql";

    private final String reportUrl;
    private final String appName;
    /** 目标项目名（平台准入校验键：项目不存在则丢弃数据） */
    private final String targetProjectName;
    /** 被测应用 HTTP 端口（显式配置；0=未知，运行期懒探测 server.port 系统属性） */
    private final int configuredAppPort;
    /** 被测应用版本号（显式配置；空=未知，运行期懒探测被测 jar MANIFEST） */
    private final String configuredAppVersion;
    private final int batchSize;
    private final int bufferSize;

    /** 运行期懒探测缓存的应用端口（-1 未探测） */
    private int resolvedAppPort = -1;
    /** 运行期懒探测缓存的应用版本（null 未探测；探测结果可能为空串） */
    private String resolvedAppVersion;

    private final Deque<Map<String, Object>> traceQueue = new ArrayDeque<>();
    private final Deque<Map<String, Object>> jvmQueue = new ArrayDeque<>();
    private final Deque<Map<String, Object>> slowSqlQueue = new ArrayDeque<>();

    private final HttpReporter http;
    private final ScheduledExecutorService scheduler;
    private final AtomicBoolean running = new AtomicBoolean(false);

    /** 失败退避状态 */
    private long backoffMs = 1000;
    private static final long MAX_BACKOFF_MS = 60_000;

    /** 平台判定项目不存在 → 永久停报（数据全部丢弃后不再发送） */
    private final AtomicBoolean stoppedByProjectGate = new AtomicBoolean(false);

    public MetricsReporter(AgentConfig config, HttpReporter http) {
        this.reportUrl = config.reportUrl;
        this.appName = config.appName;
        this.targetProjectName = config.targetProjectName;
        this.configuredAppPort = config.appPort;
        this.configuredAppVersion = config.appVersion == null ? "" : config.appVersion.trim();
        this.batchSize = config.batchSize;
        this.bufferSize = config.bufferSize;
        this.http = http;
        this.scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "perf-reporter");
            t.setDaemon(true);
            return t;
        });
    }

    /** 启动定时 flush（每 1s 检查一次） */
    public void start() {
        if (running.compareAndSet(false, true)) {
            scheduler.scheduleWithFixedDelay(this::flushAll, 1, 1, TimeUnit.SECONDS);
        }
    }

    public void stop() {
        running.set(false);
        scheduler.shutdownNow();
    }

    // ===================== 上报入口（业务线程调用，非阻塞） =====================

    /** 上报一条调用链 */
    public void reportTrace(String traceId, String app, TraceNode root, long startTimeMs) {
        if (!running.get()) {
            return;
        }
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("traceId", traceId);
        item.put("appName", app);
        item.put("targetProjectName", targetProjectName);
        item.put("rootMethod", root.method);
        // 链路总耗时 = 根节点自身耗时（父节点耗时已包含子调用，不应逐层相加）
        item.put("totalMs", root.costMs);
        item.put("startTime", startTimeMs);
        item.put("tree", toTreeMap(root));
        enqueue(traceQueue, item);
    }

    /** 上报一条 JVM 指标 */
    public void reportJvm(String app, Map<String, Object> metric) {
        if (!running.get()) {
            return;
        }
        metric.put("targetProjectName", targetProjectName);
        enqueue(jvmQueue, metric);
    }

    /** 上报一条慢 SQL（携带触发方法链与目标数据库标识，供源码定位与 EXPLAIN 分析） */
    public void reportSlowSql(String app, String sql, long costMs, long executeTimeMs,
                              List<String> methodChain, String dbHost) {
        if (!running.get()) {
            return;
        }
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("appName", app);
        item.put("targetProjectName", targetProjectName);
        item.put("sqlText", sql);
        item.put("costMs", costMs);
        item.put("source", "jdbc");
        item.put("executeTime", executeTimeMs);
        if (methodChain != null && !methodChain.isEmpty()) {
            item.put("methodChain", methodChain);
        }
        if (dbHost != null && !dbHost.isBlank()) {
            item.put("dbHost", dbHost);
        }
        enqueue(slowSqlQueue, item);
    }

    // ===================== 内部实现 =====================

    /** 入队：满则丢弃最旧 */
    private synchronized void enqueue(Deque<Map<String, Object>> queue, Map<String, Object> item) {
        if (queue.size() >= bufferSize) {
            queue.pollFirst();
        }
        queue.addLast(item);
    }

    /** 批量取出（最多 batchSize 条） */
    private synchronized List<Map<String, Object>> drain(Deque<Map<String, Object>> queue) {
        int count = Math.min(batchSize, queue.size());
        List<Map<String, Object>> list = new ArrayList<>(count);
        for (int i = 0; i < count; i++) {
            list.add(queue.pollFirst());
        }
        return list;
    }

    /** flush 所有类型队列（定时调用）；项目被平台拒绝后整体停报 */
    private void flushAll() {
        // 项目不存在（平台拒绝）：停止上报，避免无效重试与流量
        if (stoppedByProjectGate.get()) {
            return;
        }
        boolean okTrace = flush(TYPE_TRACE, traceQueue);
        boolean okJvm = flush(TYPE_JVM, jvmQueue);
        boolean okSql = flush(TYPE_SLOW_SQL, slowSqlQueue);
        if (okTrace && okJvm && okSql) {
            backoffMs = 1000;
        }
    }

    /** 发送单个类型队列，成功返回 true，失败进入退避；平台判定项目不存在则永久停报 */
    private boolean flush(String type, Deque<Map<String, Object>> queue) {
        if (backoffMs >= MAX_BACKOFF_MS || stoppedByProjectGate.get()) {
            return false;
        }
        List<Map<String, Object>> items;
        synchronized (this) {
            if (queue.isEmpty()) {
                return true;
            }
            items = drain(queue);
        }
        if (items.isEmpty()) {
            return true;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("type", type);
        body.put("appName", appName);
        body.put("targetProjectName", targetProjectName);
        int port = resolveAppPort();
        if (port > 0) {
            body.put("appPort", port);
        }
        String version = resolveAppVersion();
        if (!version.isEmpty()) {
            body.put("appVersion", version);
        }
        body.put("items", items);

        HttpReporter.PostResult result = http.postJson(reportUrl, JsonUtil.serialize(body));
        if (!result.ok()) {
            // 平台明确告知项目不存在（业务拒绝，body 含关键字）：清空队列并永久停报
            String respBody = result.body() == null ? "" : result.body();
            if (respBody.contains("项目不存在") || respBody.contains("PROJECT_NOT_FOUND")) {
                stoppedByProjectGate.set(true);
                synchronized (this) {
                    traceQueue.clear();
                    jvmQueue.clear();
                    slowSqlQueue.clear();
                }
                System.err.println("[perf-agent] 平台不存在项目「" + targetProjectName
                        + "」，上报数据被全部丢弃，agent 已停止上报。"
                        + "请核对启动参数 targetProjectName 与平台「源码仓库」中的项目名是否一致。");
                return false;
            }
            // 普通失败：数据已取出即丢弃（避免内存无限增长），进入指数退避
            backoffMs = Math.min(MAX_BACKOFF_MS, backoffMs * 2);
            try {
                Thread.sleep(Math.min(backoffMs, 2000));
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
        return result.ok();
    }

    /** 解析被测应用 HTTP 端口：显式配置优先，其次懒读 server.port 系统属性（premain 期未设置，运行期可读） */
    private int resolveAppPort() {
        if (configuredAppPort > 0) {
            return configuredAppPort;
        }
        if (resolvedAppPort > 0) {
            return resolvedAppPort;
        }
        try {
            String p = System.getProperty("server.port");
            if (p != null && p.matches("\\d+")) {
                resolvedAppPort = Integer.parseInt(p);
            }
        } catch (Throwable ignored) {
            // 系统属性读取异常按未知处理
        }
        return resolvedAppPort;
    }

    /** 解析被测应用版本：显式配置优先，否则从被测应用 jar 的 MANIFEST 读 Implementation-Version（懒探测一次缓存） */
    private String resolveAppVersion() {
        if (!configuredAppVersion.isEmpty()) {
            return configuredAppVersion;
        }
        if (resolvedAppVersion != null) {
            return resolvedAppVersion;
        }
        resolvedAppVersion = detectVersionFromManifest();
        return resolvedAppVersion;
    }

    /**
     * 从被测应用 jar 解析版本（仅当未配置 appVersion 参数时执行）：
     * 1) sun.java.command 形如 "-jar /path/app.jar --spring..." —— java -jar 启动，最精确；
     * 2) 兜底 java.class.path 中的 jar（排除 agent 自身；IDE exploded 运行时可能取到依赖 jar，返回空串更保守）。
     * 读取其 META-INF/MANIFEST.MF 的 Implementation-Version（Spring Boot repackage 默认写入 pom 版本）。
     */
    private String detectVersionFromManifest() {
        try {
            String jarPath = null;
            // 1) -jar 启动：命令行第二个参数即应用 jar
            String cmd = System.getProperty("sun.java.command", "");
            if (cmd.startsWith("-jar ")) {
                String[] parts = cmd.split("\\s+");
                if (parts.length > 1 && parts[1].endsWith(".jar")) {
                    jarPath = parts[1];
                }
            }
            // 2) 兜底：classpath 中的 jar（排除 -javaagent 自身的 jar）
            if (jarPath == null) {
                for (String p : System.getProperty("java.class.path", "").split(java.io.File.pathSeparator)) {
                    if (p.endsWith(".jar") && !p.toLowerCase().contains("perf-agent")) {
                        jarPath = p;
                        break;
                    }
                }
            }
            if (jarPath == null) {
                return "";
            }
            try (JarFile jf = new JarFile(jarPath)) {
                Manifest mf = jf.getManifest();
                if (mf == null) {
                    return "";
                }
                String v = mf.getMainAttributes().getValue(Attributes.Name.IMPLEMENTATION_VERSION);
                if (v == null || v.isBlank()) {
                    v = mf.getMainAttributes().getValue("Build-Version");
                }
                return v == null ? "" : v.trim();
            }
        } catch (Throwable ignored) {
            return "";
        }
    }

    /** TraceNode 树转 Map（便于 JSON 序列化，含每层耗时占比与开始偏移） */
    private static Map<String, Object> toTreeMap(TraceNode node) {
        Map<String, Object> map = new LinkedHashMap<>();
        map.put("method", node.method);
        map.put("costMs", node.costMs);
        map.put("offsetMs", round(node.offsetMs));
        map.put("ratio", round(node.ratio));
        if (!node.children.isEmpty()) {
            List<Object> children = new ArrayList<>();
            for (TraceNode child : node.children) {
                children.add(toTreeMap(child));
            }
            map.put("children", children);
        }
        return map;
    }

    private static double round(double v) {
        return Math.round(v * 100.0) / 100.0;
    }
}
