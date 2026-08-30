package com.perfagent.jmx;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.MetricsReporter;
import com.sun.management.OperatingSystemMXBean;

import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryUsage;
import java.lang.management.ThreadMXBean;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * JVM 指标采集器：通过 JDK JMX（Memory / GC / Thread / OS MXBean）周期采集，
 * 采集结果交由 {@link MetricsReporter} 批量上报平台。
 *
 * <p>使用独立单线程调度，不阻塞业务线程；默认 5s 采集一次。
 */
public class JvmCollector {

    private final String appName;
    private final long intervalSec;
    private final MetricsReporter reporter;
    private final ScheduledExecutorService scheduler;
    private final AtomicBoolean running = new AtomicBoolean(false);

    public JvmCollector(AgentConfig config, MetricsReporter reporter) {
        this.appName = config.appName;
        this.intervalSec = config.jmxCollectIntervalSec;
        this.reporter = reporter;
        this.scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "perf-jmx-collector");
            t.setDaemon(true);
            return t;
        });
    }

    /** 启动定时采集 */
    public void start() {
        if (running.compareAndSet(false, true)) {
            scheduler.scheduleWithFixedDelay(this::collectOnce, 1, intervalSec, TimeUnit.SECONDS);
        }
    }

    /** 停止采集（JVM 退出钩子调用） */
    public void stop() {
        running.set(false);
        scheduler.shutdownNow();
    }

    /** 采集一次 JVM 指标并上报 */
    public void collectOnce() {
        try {
            JvmMetric metric = new JvmMetric();
            metric.appName = appName;
            metric.collectTimeMs = System.currentTimeMillis();

            MemoryMXBean memory = ManagementFactory.getMemoryMXBean();
            MemoryUsage heap = memory.getHeapMemoryUsage();
            MemoryUsage nonHeap = memory.getNonHeapMemoryUsage();
            metric.heapUsedMb = heap.getUsed() / 1048576.0;
            metric.heapMaxMb = heap.getMax() > 0 ? heap.getMax() / 1048576.0 : 0;
            metric.nonHeapUsedMb = nonHeap.getUsed() / 1048576.0;

            collectGc(metric);
            collectThread(metric);
            collectCpu(metric);

            reporter.reportJvm(appName, metric.toMap());
        } catch (Throwable t) {
            // 采集失败不影响业务，忽略并等待下一周期
        }
    }

    /** 采集 GC 计数与耗时（精确名称映射区分 Young/Full） */
    private void collectGc(JvmMetric metric) {
        List<GarbageCollectorMXBean> gcs = ManagementFactory.getGarbageCollectorMXBeans();
        for (GarbageCollectorMXBean gc : gcs) {
            String name = gc.getName();
            long count = gc.getCollectionCount();
            long ms = gc.getCollectionTime();
            if (count < 0) {
                continue;
            }
            if (isYoungGc(name)) {
                metric.youngGcCount += count;
                metric.youngGcMs += ms;
            } else {
                if (!KNOWN_OLD_GC_NAMES.contains(name) && warnedUnknownGc.add(name)) {
                    // 未知收集器（新 JDK 特性等）：归入 Full 并一次性告警，避免静默误判
                    System.err.println("[perf-agent] 未识别的 GC 收集器名称「" + name
                            + "」，按 Full GC 统计（Young 集合: " + KNOWN_YOUNG_GC_NAMES + "）");
                }
                metric.fullGcCount += count;
                metric.fullGcMs += ms;
            }
        }
    }

    /** Young/Minor 收集器精确名称集合 */
    private static final Set<String> KNOWN_YOUNG_GC_NAMES = Set.of(
            "Copy",                    // Serial（Client/单核默认）
            "PS Scavenge",             // Parallel Scavenge（JDK 8 服务端默认）
            "ParNew",                  // CMS 配套
            "G1 Young Generation",     // G1（JDK 9+ 默认）
            "ZGC Cycles",              // ZGC（JDK 16+ 拆分命名；整周期归 Young 口径）
            "Shenandoah Cycles"        // Shenandoah
    );

    /** Old/Full 收集器精确名称集合（未知名称也归此处并告警） */
    private static final Set<String> KNOWN_OLD_GC_NAMES = Set.of(
            "MarkSweepCompact",        // Serial Old
            "PS MarkSweep",            // Parallel Old
            "ConcurrentMarkSweep",     // CMS
            "G1 Old Generation",       // G1（JDK 16 前命名）
            "G1 Mixed Generation",     // G1 Mixed（部分版本命名）
            "G1 Concurrent GC",        // G1 并发周期（不计停顿但归 Full 口径监控）
            "ZGC Pauses",              // ZGC 停顿（JDK 16+）
            "Shenandoah Pauses",       // Shenandoah 停顿
            "ZGC"                      // JDK 11-15 单一命名
    );

    /** 已告警过的未知收集器名称（每名称仅告警一次） */
    private final Set<String> warnedUnknownGc = ConcurrentHashMap.newKeySet();

    /**
     * 精确名称匹配 Young 收集器（包级私有供单测）。
     *
     * <p>修复旧实现按子串误判："PS MarkSweep" 含 "ps" 被误判为 Young、
     * Serial 的 "Copy" 不含任何关键字被误判为 Full、ZGC/Shenandoah 全归 Full。
     */
    boolean isYoungGc(String name) {
        return KNOWN_YOUNG_GC_NAMES.contains(name);
    }

    private void collectThread(JvmMetric metric) {
        ThreadMXBean thread = ManagementFactory.getThreadMXBean();
        metric.threadCount = thread.getThreadCount();
    }

    /** 缓存的 OS MXBean（强转获取，替代每周期 newPlatformMXBeanProxy 的无谓开销） */
    private volatile OperatingSystemMXBean cachedOsBean;

    /** 采集 CPU 使用率（通过 com.sun.management.OperatingSystemMXBean，懒缓存强转） */
    private void collectCpu(JvmMetric metric) {
        try {
            OperatingSystemMXBean os = cachedOsBean;
            if (os == null) {
                os = (OperatingSystemMXBean) ManagementFactory.getOperatingSystemMXBean();
                cachedOsBean = os;
            }
            double processLoad = os.getProcessCpuLoad();
            // getCpuLoad()（JDK 14+，替代已废弃的 getSystemCpuLoad()）：
            // 系统 CPU 占用，个别 JVM 不支持时回退 0
            double systemLoad;
            try {
                systemLoad = os.getCpuLoad();
            } catch (Throwable t) {
                systemLoad = -1;
            }
            metric.cpuUsage = processLoad >= 0 ? processLoad * 100 : 0;
            metric.systemCpu = systemLoad >= 0 ? systemLoad * 100 : 0;
        } catch (Throwable t) {
            // 部分 JVM 不支持相关 API，置 0 忽略
            metric.cpuUsage = 0;
            metric.systemCpu = 0;
        }
    }
}
