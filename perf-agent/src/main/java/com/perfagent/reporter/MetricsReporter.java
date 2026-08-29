package com.perfagent.reporter;

import com.perfagent.config.AgentConfig;
import com.perfagent.trace.TraceNode;
import com.perfagent.util.AppManifest;
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
import java.util.concurrent.atomic.AtomicLong;

/**
 * 指标上报器：本地队列缓冲 + 单线程定时 flush + 失败指数退避重试 + 超限丢弃。
 *
 * <p>低开销设计（探针影响最小化）：
 * <ul>
 *   <li><b>业务线程零转换</b>：调用链以 TraceNode 原生树直接入队（O(1)），
 *       树转 Map / JSON 序列化全部在 reporter 线程 flush 时完成；</li>
 *   <li><b>每队列独立锁</b>：trace / jvm / slowSql 三队列各自加锁，
 *       业务线程入队互不争用全局锁；</li>
 *   <li><b>trace 字节预算</b>：条数上限之外增加内存预算（节点数×256B 估算），
 *       大树洪水不会撑爆内存，超限丢最旧并计数；</li>
 *   <li><b>丢弃可观测</b>：dropped 计数随下一次上报 body 顶层带出，平台侧记录日志；</li>
 *   <li><b>平台不可达</b>：指数退避至 60s，队列满丢最旧——被测应用完全不受影响。</li>
 * </ul>
 *
 * <p>上报 body 格式：{"type":"trace|jvm|slowSql","appName":"...","items":[...],
 * "dropped":{"trace":n,"jvm":n,"slowSql":n}}（dropped 为自上次成功上报以来的累计丢弃数）
 */
public class MetricsReporter {

    /** 上报类型常量（与平台 AgentReportService 约定一致） */
    public static final String TYPE_TRACE = "trace";
    public static final String TYPE_JVM = "jvm";
    public static final String TYPE_SLOW_SQL = "slowSql";

    /** 单条 trace 字节估算：节点数 × 256B（Map + 装箱 + 字符串的保守均值），下限 1KB */
    private static final int BYTES_PER_NODE = 256;
    private static final int MIN_TRACE_BYTES = 1024;

    /** 调用链队列元素：原生树引用 + 估算字节数（转 Map 在 reporter 线程做） */
    private static final class TraceItem {
        final String traceId;
        final String app;
        final TraceNode root;
        final long startTimeMs;
        final int estBytes;

        TraceItem(String traceId, String app, TraceNode root, int nodeCount, long startTimeMs) {
            this.traceId = traceId;
            this.app = app;
            this.root = root;
            this.startTimeMs = startTimeMs;
            this.estBytes = Math.max(MIN_TRACE_BYTES, nodeCount * BYTES_PER_NODE);
        }
    }

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
    /** trace 队列字节预算（超出丢最旧） */
    private final int traceBufferBytes;

    /** 运行期懒探测缓存的应用端口（-1 未探测） */
    private int resolvedAppPort = -1;
    /** 运行期懒探测缓存的应用版本（null 未探测；探测结果可能为空串） */
    private String resolvedAppVersion;

    /** 三队列元素统一 Object（trace 存 TraceItem，jvm/slowSql 存 Map），各自独立锁 */
    private final Deque<Object> traceQueue = new ArrayDeque<>();
    private final Deque<Object> jvmQueue = new ArrayDeque<>();
    private final Deque<Object> slowSqlQueue = new ArrayDeque<>();
    /** trace 队列当前估算字节数（仅在 traceQueue 锁内变更） */
    private long traceQueueBytes = 0;

    /** 丢弃计数（自上次成功上报以来的累计；上报时取走增量） */
    private final AtomicLong droppedTrace = new AtomicLong();
    private final AtomicLong droppedJvm = new AtomicLong();
    private final AtomicLong droppedSlowSql = new AtomicLong();

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
        this.traceBufferBytes = Math.max(1024 * 1024, config.traceBufferBytes);
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

    // ===================== 上报入口（业务线程调用，非阻塞 O(1)） =====================

    /** 上报一条调用链（原生树直接入队，转 Map 在 reporter 线程做） */
    public void reportTrace(String traceId, String app, TraceNode root, int nodeCount, long startTimeMs) {
        if (!running.get()) {
            return;
        }
        TraceItem item = new TraceItem(traceId, app, root, nodeCount, startTimeMs);
        synchronized (traceQueue) {
            // 条数与字节预算双重限制，超限丢最旧
            while (traceQueue.size() >= bufferSize || traceQueueBytes + item.estBytes > traceBufferBytes) {
                Object old = traceQueue.pollFirst();
                if (old == null) {
                    break;
                }
                traceQueueBytes -= ((TraceItem) old).estBytes;
                droppedTrace.incrementAndGet();
            }
            traceQueue.addLast(item);
            traceQueueBytes += item.estBytes;
        }
    }

    /** 上报一条 JVM 指标 */
    public void reportJvm(String app, Map<String, Object> metric) {
        if (!running.get()) {
            return;
        }
        metric.put("targetProjectName", targetProjectName);
        synchronized (jvmQueue) {
            dropOldest(jvmQueue, droppedJvm);
            jvmQueue.addLast(metric);
        }
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
        synchronized (slowSqlQueue) {
            dropOldest(slowSqlQueue, droppedSlowSql);
            slowSqlQueue.addLast(item);
        }
    }

    /** 条数超限丢最旧（jvm/slowSql 队列，条数即体积的良好代理） */
    private void dropOldest(Deque<Object> queue, AtomicLong dropped) {
        if (queue.size() >= bufferSize) {
            queue.pollFirst();
            dropped.incrementAndGet();
        }
    }

    // ===================== 内部实现（reporter 线程） =====================

    /** flush 所有类型队列（定时调用）；项目被平台拒绝后整体停报 */
    private void flushAll() {
        // 项目不存在（平台拒绝）：停止上报，避免无效重试与流量
        if (stoppedByProjectGate.get()) {
            return;
        }
        boolean okTrace = flushTrace();
        boolean okJvm = flushSimple(TYPE_JVM, jvmQueue);
        boolean okSql = flushSimple(TYPE_SLOW_SQL, slowSqlQueue);
        if (okTrace && okJvm && okSql) {
            backoffMs = 1000;
        }
    }

    /** 发送 trace 队列（锁内仅 drain，树转 Map 在锁外做，不阻塞业务线程入队） */
    private boolean flushTrace() {
        if (backoffMs >= MAX_BACKOFF_MS || stoppedByProjectGate.get()) {
            return false;
        }
        List<TraceItem> items;
        synchronized (traceQueue) {
            if (traceQueue.isEmpty()) {
                return true;
            }
            int count = Math.min(batchSize, traceQueue.size());
            items = new ArrayList<>(count);
            for (int i = 0; i < count; i++) {
                TraceItem t = (TraceItem) traceQueue.pollFirst();
                traceQueueBytes -= t.estBytes;
                items.add(t);
            }
        }
        if (items.isEmpty()) {
            return true;
        }
        // 树转 Map 在 reporter 线程（业务线程只入队原生树）
        List<Map<String, Object>> converted = new ArrayList<>(items.size());
        for (TraceItem t : items) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("traceId", t.traceId);
            item.put("appName", t.app);
            item.put("targetProjectName", targetProjectName);
            item.put("rootMethod", t.root.method);
            // 链路总耗时 = 根节点自身耗时（父节点耗时已包含子调用，不应逐层相加）
            item.put("totalMs", t.root.costMs);
            item.put("startTime", t.startTimeMs);
            item.put("tree", toTreeMap(t.root));
            converted.add(item);
        }
        return post(TYPE_TRACE, converted);
    }

    /** 发送 jvm / slowSql 队列（元素已是 Map，直接批量） */
    private boolean flushSimple(String type, Deque<Object> queue) {
        if (backoffMs >= MAX_BACKOFF_MS || stoppedByProjectGate.get()) {
            return false;
        }
        List<Map<String, Object>> items;
        synchronized (queue) {
            if (queue.isEmpty()) {
                return true;
            }
            int count = Math.min(batchSize, queue.size());
            items = new ArrayList<>(count);
            for (int i = 0; i < count; i++) {
                items.add((Map<String, Object>) queue.pollFirst());
            }
        }
        if (items.isEmpty()) {
            return true;
        }
        return post(type, items);
    }

    /** 组装 body 并 POST；失败进入退避；平台判定项目不存在则永久停报 */
    @SuppressWarnings("unchecked")
    private boolean post(String type, List<Map<String, Object>> items) {
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
        // 丢弃计数（增量取走，供平台侧数据完整性观测）
        long dt = droppedTrace.getAndSet(0);
        long dj = droppedJvm.getAndSet(0);
        long ds = droppedSlowSql.getAndSet(0);
        if (dt > 0 || dj > 0 || ds > 0) {
            body.put("dropped", Map.of("trace", dt, "jvm", dj, "slowSql", ds));
        }

        HttpReporter.PostResult result = http.postJson(reportUrl, JsonUtil.serialize(body));
        if (!result.ok()) {
            // 平台明确告知项目不存在（业务拒绝，body 含关键字）：清空队列并永久停报
            String respBody = result.body() == null ? "" : result.body();
            if (respBody.contains("项目不存在") || respBody.contains("PROJECT_NOT_FOUND")) {
                stoppedByProjectGate.set(true);
                synchronized (traceQueue) {
                    traceQueue.clear();
                    traceQueueBytes = 0;
                }
                synchronized (jvmQueue) {
                    jvmQueue.clear();
                }
                synchronized (slowSqlQueue) {
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

    /** 解析被测应用版本：显式配置优先，否则读被测 jar MANIFEST（AppManifest 懒探测一次缓存） */
    private String resolveAppVersion() {
        if (!configuredAppVersion.isEmpty()) {
            return configuredAppVersion;
        }
        if (resolvedAppVersion == null) {
            resolvedAppVersion = AppManifest.implementationVersion();
        }
        return resolvedAppVersion;
    }

    /** TraceNode 树转 Map（便于 JSON 序列化，含每层耗时占比与开始偏移）——仅在 reporter 线程执行 */
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
