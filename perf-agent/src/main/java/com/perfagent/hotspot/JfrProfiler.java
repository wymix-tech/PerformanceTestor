package com.perfagent.hotspot;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.MetricsReporter;
import jdk.jfr.consumer.RecordedClass;
import jdk.jfr.consumer.RecordedFrame;
import jdk.jfr.consumer.RecordedStackTrace;
import jdk.jfr.consumer.RecordingStream;

import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.LongAdder;

/**
 * JFR 低开销连续剖析器（P2-1）：替代 HotspotSampler 的高频 Safepoint 抓栈方案。
 *
 * <p>基于 JDK 17 内置 {@link RecordingStream} 开启三类事件，产出标准折叠栈
 * （Folded Stacks，每行 {@code frame1;frame2;...;frameN count}）：
 * <ul>
 *   <li>{@code jdk.ExecutionSample}（周期 {@code jfrCpuIntervalMs}）→ profileType=cpu；</li>
 *   <li>{@code jdk.ObjectAllocationSample}（节流 {@code jfrAllocThrottle}/s）→
 *       profileType=alloc（weight 为分配字节数，按字节累加）；</li>
 *   <li>{@code jdk.JavaMonitorEnter}（阈值 {@code jfrLockThresholdMs}）→
 *       profileType=lock（按阻塞耗时毫秒累加）。</li>
 * </ul>
 *
 * <p>低开销与安全边界（探针红线）：
 * <ul>
 *   <li>JFR 事件由 JVM 内部安全点批处理产出，开销比全线程 getThreadInfo 低一个量级；</li>
 *   <li>事件回调线程只做"拼串 + LongAdder 累加"，绝不做网络 IO / 复杂正则 / 大对象序列化；</li>
 *   <li>单条栈深度上限 {@value #MAX_STACK_DEPTH}，每类唯一栈条数上限
 *       {@value #MAX_STACKS}（超限丢弃新栈，防深递归/动态生成类撑爆内存）；</li>
 *   <li>{@code setMaxAge(60s)} + {@code setMaxSize(32MB)} 防长时压测在临时目录堆积 JFR 磁盘段；</li>
 *   <li>{@link #stop()} 关闭事件流并停上报定时器，由 AgentPremain shutdown hook 调用。</li>
 * </ul>
 *
 * <p>双轨上报兼容：周期快照时一方面调 {@code reporter.reportProfile(...)} 上报完整折叠栈；
 * 另一方面从 cpu 栈提取叶帧 Top N 兼容调用原有 {@code reporter.reportHotspot(...)}，
 * 确保既有热点排行与 AI hotspot_query 零破坏。
 */
public final class JfrProfiler implements ProfilerBackend {

    /** 单条折叠栈深度上限（超限保留靠近叶帧的最后 N 帧） */
    static final int MAX_STACK_DEPTH = 64;
    /** 每种 profileType 唯一栈条数上限（超限丢弃新栈防 OOM） */
    static final int MAX_STACKS = 4096;

    /** profileType 常量（与平台 profile_snapshot.profile_type 约定一致） */
    public static final String TYPE_CPU = "cpu";
    public static final String TYPE_ALLOC = "alloc";
    public static final String TYPE_LOCK = "lock";

    /** agent 自身包前缀（折叠栈过滤，探针帧不污染业务热点） */
    private static final String[] FILTERED_PREFIXES = {
            "com.perfagent.", "net.bytebuddy.", "jdk.jfr.",
    };

    private final AgentConfig config;
    private final MetricsReporter reporter;
    /** profileType → (折叠栈 → 计数)，JFR 事件回调线程写入，上报线程快照清零 */
    private final Map<String, ConcurrentHashMap<String, LongAdder>> stacks = new ConcurrentHashMap<>();
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final ScheduledExecutorService scheduler;
    private volatile RecordingStream stream;

    public JfrProfiler(AgentConfig config, MetricsReporter reporter) {
        this.config = config;
        this.reporter = reporter;
        this.scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "perf-jfr-profiler");
            t.setDaemon(true);
            return t;
        });
    }

    @Override
    public void start() {
        if (!running.compareAndSet(false, true)) {
            return;
        }
        try {
            RecordingStream rs = new RecordingStream();
            rs.setMaxAge(Duration.ofSeconds(60));
            rs.setMaxSize(32 * 1024 * 1024);
            rs.enable("jdk.ExecutionSample")
                    .with("period", Math.max(1, config.jfrCpuIntervalMs) + "ms");
            rs.enable("jdk.ObjectAllocationSample")
                    .with("throttle", Math.max(1, config.jfrAllocThrottle) + "/s");
            rs.enable("jdk.JavaMonitorEnter")
                    .with("threshold", Math.max(1, config.jfrLockThresholdMs) + "ms");
            rs.onEvent("jdk.ExecutionSample",
                    e -> onSample(TYPE_CPU, e.getStackTrace(), 1L));
            rs.onEvent("jdk.ObjectAllocationSample",
                    e -> onSample(TYPE_ALLOC, e.getStackTrace(), e.getLong("weight")));
            rs.onEvent("jdk.JavaMonitorEnter",
                    e -> onSample(TYPE_LOCK, e.getStackTrace(),
                            Math.max(1, e.getDuration().toMillis())));
            rs.startAsync();
            this.stream = rs;
        } catch (Throwable t) {
            running.set(false);
            System.err.println("[perf-agent] JFR 连续剖析启动失败（JDK 不支持或事件不可用），"
                    + "已跳过：" + t);
            return;
        }
        scheduler.scheduleWithFixedDelay(this::report,
                Math.max(1, config.hotspotReportSec), Math.max(1, config.hotspotReportSec),
                TimeUnit.SECONDS);
    }

    /** 事件处理热路径：仅折叠栈拼串 + LongAdder 累加，无 IO / 锁等待 / 复杂正则 */
    private void onSample(String profileType, RecordedStackTrace st, long weight) {
        if (st == null || weight <= 0) {
            return;
        }
        try {
            List<RecordedFrame> frames = st.getFrames();
            // 根帧 → 叶帧拼接，过滤 agent / 字节码库 / JFR 内部帧
            List<String> kept = new ArrayList<>(frames.size());
            for (RecordedFrame f : frames) {
                String frame = frameText(f);
                if (frame != null) {
                    kept.add(frame);
                }
            }
            if (kept.isEmpty()) {
                return;
            }
            if (kept.size() > MAX_STACK_DEPTH) {
                // 超限保留靠近叶帧的最后 64 帧（叶帧是热点定位的关键）
                kept = kept.subList(kept.size() - MAX_STACK_DEPTH, kept.size());
            }
            String folded = String.join(";", kept);
            ConcurrentHashMap<String, LongAdder> bucket =
                    stacks.computeIfAbsent(profileType, k -> new ConcurrentHashMap<>());
            if (bucket.size() >= MAX_STACKS && !bucket.containsKey(folded)) {
                return; // 超限：丢弃新栈（既有栈继续累加），轻微竞态可接受
            }
            bucket.computeIfAbsent(folded, k -> new LongAdder()).add(weight);
        } catch (Throwable ignored) {
            // 事件解析异常静默（JVM 关闭期等），绝不影响被测应用
        }
    }

    /** 单帧文本：{@code pkg.Class.method}；无 Java 方法名或属过滤前缀时返回 null */
    private static String frameText(RecordedFrame f) {
        try {
            if (!f.isJavaFrame()) {
                return null;
            }
            RecordedClass cls = f.getMethod().getType();
            if (cls == null || cls.getName() == null) {
                return null;
            }
            String text = cls.getName() + "." + f.getMethod().getName();
            for (String p : FILTERED_PREFIXES) {
                if (text.startsWith(p)) {
                    return null;
                }
            }
            return text;
        } catch (Throwable ignored) {
            return null;
        }
    }

    /** 周期上报：快照 + 清零；cpu 栈叶帧 Top N 兼容 reportHotspot（既有排行零破坏） */
    private void report() {
        try {
            long sampleTimeMs = System.currentTimeMillis();
            for (Map.Entry<String, ConcurrentHashMap<String, LongAdder>> en
                    : stacks.entrySet()) {
                ConcurrentHashMap<String, LongAdder> bucket = en.getValue();
                if (bucket.isEmpty()) {
                    continue;
                }
                List<Map.Entry<String, LongAdder>> entries = new ArrayList<>(bucket.entrySet());
                bucket.clear();
                long totalSamples = 0;
                StringBuilder sb = new StringBuilder();
                for (Map.Entry<String, LongAdder> e : entries) {
                    long cnt = e.getValue().sum();
                    if (cnt <= 0) {
                        continue;
                    }
                    totalSamples += cnt;
                    if (sb.length() > 0) {
                        sb.append('\n');
                    }
                    sb.append(e.getKey()).append(' ').append(cnt);
                }
                if (totalSamples <= 0) {
                    continue;
                }
                String type = en.getKey();
                reporter.reportProfile(type, sb.toString(), totalSamples, sampleTimeMs);
                if (TYPE_CPU.equals(type)) {
                    reportLeafHotspot(entries, sampleTimeMs);
                }
            }
        } catch (Throwable ignored) {
            // 上报异常静默（下周期重试）
        }
    }

    /** cpu 折叠栈叶帧提取 Top N（兼容既有 hotspot_method 表与 AI hotspot_query） */
    private void reportLeafHotspot(List<Map.Entry<String, LongAdder>> stackEntries,
                                   long sampleTimeMs) {
        Map<String, LongAdder> leaves = new HashMap<>();
        for (Map.Entry<String, LongAdder> e : stackEntries) {
            String folded = e.getKey();
            int idx = folded.lastIndexOf(';');
            String leaf = idx >= 0 ? folded.substring(idx + 1) : folded;
            leaves.computeIfAbsent(leaf, k -> new LongAdder()).add(e.getValue().sum());
        }
        List<Map.Entry<String, LongAdder>> top = new ArrayList<>(leaves.entrySet());
        top.sort((a, b) -> Long.compare(b.getValue().sum(), a.getValue().sum()));
        int limit = Math.min(Math.max(1, config.hotspotTopN), top.size());
        List<Map<String, Object>> items = new ArrayList<>(limit);
        for (int i = 0; i < limit; i++) {
            Map.Entry<String, LongAdder> e = top.get(i);
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("method", e.getKey());
            m.put("samples", e.getValue().sum());
            items.add(m);
        }
        if (!items.isEmpty()) {
            reporter.reportHotspot(items);
        }
    }

    @Override
    public void stop() {
        if (!running.compareAndSet(true, false)) {
            return; // 幂等：未启动或已停止
        }
        scheduler.shutdownNow();
        RecordingStream rs = stream;
        stream = null;
        if (rs != null) {
            try {
                // close 会停止后台消费线程并释放磁盘临时 chunk
                rs.close();
            } catch (Throwable ignored) {
                // 关闭异常静默（JVM 退出期）
            }
        }
        // 尾窗兜底：停止前快照上报最后一次聚合数据，避免最近一个上报周期数据丢失
        report();
    }

    /** 是否已启动（测试与诊断用） */
    public boolean isRunning() {
        return running.get();
    }
}
