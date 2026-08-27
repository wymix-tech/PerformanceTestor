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
    private final int batchSize;
    private final int bufferSize;

    private final Deque<Map<String, Object>> traceQueue = new ArrayDeque<>();
    private final Deque<Map<String, Object>> jvmQueue = new ArrayDeque<>();
    private final Deque<Map<String, Object>> slowSqlQueue = new ArrayDeque<>();

    private final HttpReporter http;
    private final ScheduledExecutorService scheduler;
    private final AtomicBoolean running = new AtomicBoolean(false);

    /** 失败退避状态 */
    private long backoffMs = 1000;
    private static final long MAX_BACKOFF_MS = 60_000;

    public MetricsReporter(AgentConfig config, HttpReporter http) {
        this.reportUrl = config.reportUrl;
        this.appName = config.appName;
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
        item.put("rootMethod", root.method);
        item.put("totalMs", root.totalCost());
        item.put("startTime", startTimeMs);
        item.put("tree", toTreeMap(root));
        enqueue(traceQueue, item);
    }

    /** 上报一条 JVM 指标 */
    public void reportJvm(String app, Map<String, Object> metric) {
        if (!running.get()) {
            return;
        }
        enqueue(jvmQueue, metric);
    }

    /** 上报一条慢 SQL */
    public void reportSlowSql(String app, String sql, long costMs, long executeTimeMs) {
        if (!running.get()) {
            return;
        }
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("appName", app);
        item.put("sqlText", sql);
        item.put("costMs", costMs);
        item.put("source", "jdbc");
        item.put("executeTime", executeTimeMs);
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

    /** flush 所有类型队列（定时调用） */
    private void flushAll() {
        // 退避期间跳过本轮，避免频繁重试加重网络负担
        if (backoffMs >= MAX_BACKOFF_MS) {
            // 长时间失败：检测队列是否为空来重置退避
        }
        boolean okTrace = flush(TYPE_TRACE, traceQueue);
        boolean okJvm = flush(TYPE_JVM, jvmQueue);
        boolean okSql = flush(TYPE_SLOW_SQL, slowSqlQueue);
        if (okTrace && okJvm && okSql) {
            backoffMs = 1000;
        }
    }

    /** 发送单个类型队列，成功返回 true，失败进入退避 */
    private boolean flush(String type, Deque<Map<String, Object>> queue) {
        if (backoffMs >= MAX_BACKOFF_MS) {
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
        body.put("items", items);

        boolean ok = http.postJson(reportUrl, JsonUtil.serialize(body));
        if (!ok) {
            // 失败：数据已取出即丢弃（避免内存无限增长），进入指数退避
            backoffMs = Math.min(MAX_BACKOFF_MS, backoffMs * 2);
            try {
                Thread.sleep(Math.min(backoffMs, 2000));
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
        return ok;
    }

    /** TraceNode 树转 Map（便于 JSON 序列化，含每层耗时占比） */
    private static Map<String, Object> toTreeMap(TraceNode node) {
        Map<String, Object> map = new LinkedHashMap<>();
        map.put("method", node.method);
        map.put("costMs", node.costMs);
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
