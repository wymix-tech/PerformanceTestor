package com.perfagent.hotspot;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.MetricsReporter;

import java.lang.management.ManagementFactory;
import java.lang.management.ThreadInfo;
import java.lang.management.ThreadMXBean;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.LongAdder;

/**
 * 方法级 CPU 热点弱采样器：周期抓取 RUNNABLE 线程栈顶帧并聚合，
 * 回答"CPU 烧在哪"（调用链只能回答"耗时在哪"，CPU 密集与等待耗时需分别观测）。
 *
 * <p>开销控制：
 * <ul>
 *   <li>默认 10ms 采样周期（{@code hotspotIntervalMs}），每次仅抓栈顶 8 帧（热点集中在叶子帧）；</li>
 *   <li>帧基数上限 {@value #MAX_FRAMES}（超出后新帧丢弃，防深递归/动态生成类撑爆内存）；</li>
 *   <li>聚合结构 {@code ConcurrentHashMap<frame, LongAdder>}，采样线程无锁累加；</li>
 *   <li>{@code hotspotEnabled} 默认关闭——需要时开启，避免常态开销。</li>
 * </ul>
 *
 * <p>语义说明：RUNNABLE 包含少量不可中断 IO 等待（Linux），统计为"近似 CPU 帧命中"；
 * 与 async-profiler 相比无 safepoint 偏差以外的系统性误差，作为低成本兜底方案
 * （profilerMode=threadmx 或 JFR 不可用时的回退后端）。
 */
public class HotspotSampler implements ProfilerBackend {

    /** 聚合帧数上限（防内存洪水） */
    private static final int MAX_FRAMES = 8192;
    /** 每线程抓取的栈深度（热点集中在栈顶叶子帧） */
    private static final int STACK_DEPTH = 8;

    private final AgentConfig config;
    private final MetricsReporter reporter;
    private final ThreadMXBean threads = ManagementFactory.getThreadMXBean();
    private final ConcurrentHashMap<String, LongAdder> frameCounts = new ConcurrentHashMap<>();
    private final ScheduledExecutorService scheduler;
    private final AtomicBoolean running = new AtomicBoolean(false);
    /** 采样周期下限（防呆）：低于该值的全线程 getThreadInfo 会造成 Safepoint 风暴 */
    static final int MIN_INTERVAL_MS = 20;

    /** 防呆生效后的实际采样周期（≥ 20ms） */
    final int effectiveIntervalMs;

    public HotspotSampler(AgentConfig config, MetricsReporter reporter) {
        this.config = config;
        this.reporter = reporter;
        // P1-5 Safepoint 防呆：配置 < 20ms 时上调到 20ms 并一次性 stderr 告警。
        // 高频全线程 getThreadInfo 需要停顿所有线程（Safepoint），采样越密停顿越频繁，
        // 会直接放大被测应用 P99 抖动（观测者效应）。P2 预留替换为 JDK Flight Recorder
        // （JFR）连续剖析（ExecutionSample 事件由 JVM 内部安全点批处理，开销低一个量级）。
        int requested = config.hotspotIntervalMs;
        this.effectiveIntervalMs = Math.max(MIN_INTERVAL_MS, requested);
        if (requested < MIN_INTERVAL_MS) {
            System.err.println("[perf-agent] hotspotIntervalMs=" + requested
                    + "ms 低于安全下限 " + MIN_INTERVAL_MS + "ms（全线程 getThreadInfo "
                    + "高频触发 Safepoint 风暴，放大被测应用停顿），已上调至 "
                    + this.effectiveIntervalMs + "ms；持续剖析建议改用 JFR（P2 预留）");
        }
        this.scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "perf-hotspot");
            t.setDaemon(true);
            return t;
        });
    }

    public void start() {
        if (running.compareAndSet(false, true)) {
            scheduler.scheduleWithFixedDelay(this::sample,
                    500, effectiveIntervalMs, TimeUnit.MILLISECONDS);
            scheduler.scheduleWithFixedDelay(this::report,
                    config.hotspotReportSec, Math.max(1, config.hotspotReportSec), TimeUnit.SECONDS);
        }
    }

    public void stop() {
        running.set(false);
        scheduler.shutdownNow();
    }

    /** 单次采样：抓取全部 RUNNABLE 线程栈顶帧聚合（异常静默——采样失败不影响被测应用） */
    private void sample() {
        try {
            long[] ids = threads.getAllThreadIds();
            ThreadInfo[] infos = threads.getThreadInfo(ids, STACK_DEPTH);
            for (ThreadInfo info : infos) {
                if (info == null || info.getThreadState() != Thread.State.RUNNABLE) {
                    continue;
                }
                if (isAgentThread(info.getThreadName())) {
                    continue;
                }
                // 同一线程同帧去重（递归调用只记一次命中，防止单深递归线程撑爆计数）
                Set<String> seen = new HashSet<>();
                for (StackTraceElement el : info.getStackTrace()) {
                    String cls = el.getClassName();
                    if (cls.startsWith("com.perfagent.") || cls.startsWith("net.bytebuddy.")) {
                        continue;
                    }
                    String frame = cls + "." + el.getMethodName();
                    if (!seen.add(frame)) {
                        continue;
                    }
                    countFrame(frame);
                }
            }
        } catch (Throwable ignored) {
            // 采样异常（JVM 关闭期等）静默
        }
    }

    /** 帧计数（有界插入：帧基数达上限后新帧丢弃） */
    private void countFrame(String frame) {
        if (frameCounts.size() >= MAX_FRAMES && !frameCounts.containsKey(frame)) {
            return; // 超限：丢弃新帧（既有帧继续累加），轻微竞态可接受
        }
        frameCounts.computeIfAbsent(frame, k -> new LongAdder()).increment();
    }

    /** 周期上报：快照 + 清零 + topN 批量上报 */
    private void report() {
        try {
            if (frameCounts.isEmpty()) {
                return;
            }
            List<Map<String, Object>> top = new ArrayList<>();
            List<Map.Entry<String, LongAdder>> entries = new ArrayList<>(frameCounts.entrySet());
            frameCounts.clear();
            entries.sort((a, b) -> Long.compare(b.getValue().sum(), a.getValue().sum()));
            int limit = Math.min(Math.max(1, config.hotspotTopN), entries.size());
            for (int i = 0; i < limit; i++) {
                Map.Entry<String, LongAdder> e = entries.get(i);
                Map<String, Object> m = new HashMap<>();
                m.put("method", e.getKey());
                m.put("samples", e.getValue().sum());
                top.add(m);
            }
            reporter.reportHotspot(top);
        } catch (Throwable ignored) {
            // 上报异常静默（下周期重试）
        }
    }

    /** 排除 agent 自身线程（reporter / sampler / shutdown hook） */
    private static boolean isAgentThread(String name) {
        return name != null && (name.startsWith("perf-") || name.startsWith("perf-hotspot"));
    }
}
