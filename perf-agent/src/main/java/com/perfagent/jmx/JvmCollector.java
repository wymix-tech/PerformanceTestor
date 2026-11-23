package com.perfagent.jmx;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.MetricsReporter;
import com.sun.management.GarbageCollectionNotificationInfo;
import com.sun.management.OperatingSystemMXBean;

import java.lang.management.BufferPoolMXBean;
import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryPoolMXBean;
import java.lang.management.MemoryUsage;
import java.lang.management.ThreadInfo;
import java.lang.management.ThreadMXBean;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
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
 * <p>P1-5 扩展：连接池（HikariCP）/容器线程池（Tomcat）MBean 只读探测与
 * 内存/线程细分，产出 {@code extraMetrics} 可选扩展 JSON（服务端独立扩展列落库，
 * 旧表字段零破坏）。全部按 ObjectName 名称探测，零编译期依赖。
 *
 * <p>使用独立单线程调度，不阻塞业务线程；默认 5s 采集一次。
 */
public class JvmCollector {

    private final String appName;
    private final long intervalSec;
    private final MetricsReporter reporter;
    private final ScheduledExecutorService scheduler;
    private final AtomicBoolean running = new AtomicBoolean(false);
    /** 单次 GC 停顿明细跟踪器（P2-2：注册到 GarbageCollectorMXBean 的 NotificationEmitter） */
    private final GcPauseTracker gcPauseTracker = new GcPauseTracker();
    /** 已注册监听的 GC Bean（stop 时注销，防重复注册/泄漏） */
    private final java.util.List<javax.management.NotificationEmitter> gcEmitters = new java.util.ArrayList<>();

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

    /** 启动定时采集（并注册 GC 停顿监听） */
    public void start() {
        if (running.compareAndSet(false, true)) {
            registerGcPauseListener();
            scheduler.scheduleWithFixedDelay(this::collectOnce, 1, intervalSec, TimeUnit.SECONDS);
        }
    }

    /** 停止采集（注销 GC 监听，JVM 退出钩子调用） */
    public void stop() {
        running.set(false);
        scheduler.shutdownNow();
        unregisterGcPauseListener();
    }

    /**
     * 注册 GC 停顿监听（P2-2）：对实现 {@code NotificationEmitter} 的收集器 Bean 注册
     * {@code GARBAGE_COLLECTION_NOTIFICATION} 监听。重复 start 幂等（仅注册一次）；
     * 注册失败静默（个别 JVM 不支持时退化为仅有累计计数）。
     */
    private void registerGcPauseListener() {
        if (!gcEmitters.isEmpty()) {
            return;
        }
        try {
            for (GarbageCollectorMXBean gc : ManagementFactory.getGarbageCollectorMXBeans()) {
                if (gc instanceof javax.management.NotificationEmitter emitter) {
                    try {
                        emitter.addNotificationListener(gcPauseTracker, notification ->
                                GarbageCollectionNotificationInfo.GARBAGE_COLLECTION_NOTIFICATION
                                        .equals(notification.getType()), null);
                        gcEmitters.add(emitter);
                    } catch (Throwable ignored) {
                        // 单个 Bean 注册失败不影响其余
                    }
                }
            }
        } catch (Throwable ignored) {
            // 监听注册失败：退化为仅有 GC 累计计数（P1 及以前行为）
        }
    }

    /** 注销 GC 停顿监听（stop 幂等） */
    private void unregisterGcPauseListener() {
        for (javax.management.NotificationEmitter emitter : gcEmitters) {
            try {
                emitter.removeNotificationListener(gcPauseTracker);
            } catch (Throwable ignored) {
                // Bean 已不可用：静默
            }
        }
        gcEmitters.clear();
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
            // P2-2：单次 GC 停顿明细并入 extraMetrics（maxGcPauseMs / lastGcCause / lastGcAction）
            Map<String, Object> extra = collectExtraMetrics();
            Map<String, Object> gcDetail = gcPauseTracker.drain();
            if (gcDetail != null) {
                if (extra == null) {
                    extra = new LinkedHashMap<>();
                }
                extra.putAll(gcDetail);
            }
            metric.extraMetrics = extra == null || extra.isEmpty() ? null : extra;

            reporter.reportJvm(appName, metric.toMap());
        } catch (Throwable t) {
            // 采集失败不影响业务，忽略并等待下一周期
        }
    }

    /**
     * 扩展指标采集（P1-5，可选上报）：HikariCP 连接池、Tomcat 容器线程池、
     * 直接内存 / Metaspace、BLOCKED/WAITING 线程计数。
     *
     * <p>全部只读探测：平台 {@code getPlatformMBeanServer()} 按 ObjectName 通配符
     * 名称匹配（Hikari/Tomcat 类无需在 classpath），类或 MBean 不存在时静默缺省；
     * 线程状态仅计数不抓栈（避免高频 Safepoint）。任何异常吞掉返回 null（不上报字段）。
     */
    private Map<String, Object> collectExtraMetrics() {
        Map<String, Object> extra = new LinkedHashMap<>();
        try { // HikariCP 连接池：com.zaxxer.hikari:type=Pool (*)
            Set<javax.management.ObjectName> pools = mbeanServer()
                    .queryNames(new javax.management.ObjectName("com.zaxxer.hikari:type=Pool,*"), null);
            if (!pools.isEmpty()) {
                Map<String, Object> hikari = new LinkedHashMap<>();
                int active = 0;
                int awaiting = 0;
                int total = 0;
                for (javax.management.ObjectName on : pools) {
                    active += attrInt(on, "ActiveConnections");
                    awaiting += attrInt(on, "ThreadsAwaitingConnection");
                    total += attrInt(on, "TotalConnections");
                }
                hikari.put("activeConnections", active);
                hikari.put("threadsAwaitingConnection", awaiting);
                hikari.put("totalConnections", total);
                extra.put("hikari", hikari);
            }
        } catch (Throwable ignored) {
            // HikariCP 不在 classpath / MBean 缺失：静默缺省
        }
        try { // Tomcat 线程池：Tomcat:type=ThreadPool,name=*（多池取峰值）
            Set<javax.management.ObjectName> pools = mbeanServer()
                    .queryNames(new javax.management.ObjectName("Tomcat:type=ThreadPool,*"), null);
            if (!pools.isEmpty()) {
                Map<String, Object> tomcat = new LinkedHashMap<>();
                int busy = 0;
                int count = 0;
                int max = 0;
                for (javax.management.ObjectName on : pools) {
                    busy = Math.max(busy, attrInt(on, "currentThreadsBusy"));
                    count = Math.max(count, attrInt(on, "currentThreadCount"));
                    max = Math.max(max, attrInt(on, "maxThreads"));
                }
                tomcat.put("busyThreads", busy);
                tomcat.put("threadCount", count);
                tomcat.put("maxThreads", max);
                extra.put("tomcat", tomcat);
            }
        } catch (Throwable ignored) {
            // 非 Tomcat 容器：静默缺省
        }
        try { // 直接内存（BufferPoolMXBean：direct used）
            long direct = 0;
            for (BufferPoolMXBean pool : ManagementFactory.getPlatformMXBeans(BufferPoolMXBean.class)) {
                if ("direct".equalsIgnoreCase(pool.getName())) {
                    direct = pool.getMemoryUsed();
                    break;
                }
            }
            if (direct > 0) {
                extra.put("directMemoryMb", Math.round(direct / 1048576.0 * 10.0) / 10.0);
            }
        } catch (Throwable ignored) {
            // 平台不支持：静默缺省
        }
        try { // Metaspace（内存池 usage.used）
            for (MemoryPoolMXBean pool : ManagementFactory.getMemoryPoolMXBeans()) {
                if (pool.getName().contains("Metaspace") && pool.getUsage() != null) {
                    extra.put("metaspaceUsedMb",
                            Math.round(pool.getUsage().getUsed() / 1048576.0 * 10.0) / 10.0);
                    break;
                }
            }
        } catch (Throwable ignored) {
            // 平台不支持：静默缺省
        }
        try { // 线程状态细分：仅计数，不抓栈（避免 Safepoint 风暴）
            long[] ids = ManagementFactory.getThreadMXBean().getAllThreadIds();
            ThreadInfo[] infos = ManagementFactory.getThreadMXBean().getThreadInfo(ids);
            int blocked = 0;
            int waiting = 0;
            for (ThreadInfo info : infos) {
                if (info == null) {
                    continue;
                }
                Thread.State st = info.getThreadState();
                if (st == Thread.State.BLOCKED) {
                    blocked++;
                } else if (st == Thread.State.WAITING || st == Thread.State.TIMED_WAITING) {
                    waiting++;
                }
            }
            Map<String, Object> states = new LinkedHashMap<>();
            states.put("blocked", blocked);
            states.put("waiting", waiting);
            extra.put("threadStates", states);
        } catch (Throwable ignored) {
            // 平台不支持：静默缺省
        }
        return extra.isEmpty() ? null : extra;
    }

    /** MBean 属性只读读取（类型不符/属性缺失返回 0，绝不抛栈） */
    private int attrInt(javax.management.ObjectName name, String attr) {
        try {
            Object v = mbeanServer().getAttribute(name, attr);
            return v instanceof Number n ? n.intValue() : 0;
        } catch (Throwable ignored) {
            return 0;
        }
    }

    private javax.management.MBeanServer mbeanServer() {
        return ManagementFactory.getPlatformMBeanServer();
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
