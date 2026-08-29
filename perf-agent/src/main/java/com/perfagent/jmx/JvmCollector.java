package com.perfagent.jmx;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.MetricsReporter;
import com.sun.management.OperatingSystemMXBean;

import javax.management.MBeanServer;
import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryUsage;
import java.lang.management.ThreadMXBean;
import java.util.List;
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

    /** 采集 GC 计数与耗时（按名称区分 Young/Full） */
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
                metric.fullGcCount += count;
                metric.fullGcMs += ms;
            }
        }
    }

    private boolean isYoungGc(String name) {
        String lower = name.toLowerCase();
        return lower.contains("young") || lower.contains("eden") || lower.contains("scavenge")
                || lower.contains("new") || lower.contains("ps");
    }

    private void collectThread(JvmMetric metric) {
        ThreadMXBean thread = ManagementFactory.getThreadMXBean();
        metric.threadCount = thread.getThreadCount();
    }

    /** 采集 CPU 使用率（通过 com.sun.management.OperatingSystemMXBean） */
    private void collectCpu(JvmMetric metric) {
        MBeanServer server = ManagementFactory.getPlatformMBeanServer();
        try {
            Object osBean = ManagementFactory.newPlatformMXBeanProxy(
                    server, ManagementFactory.OPERATING_SYSTEM_MXBEAN_NAME, OperatingSystemMXBean.class);
            OperatingSystemMXBean os = (OperatingSystemMXBean) osBean;
            double processLoad = os.getProcessCpuLoad();
            double systemLoad = os.getSystemCpuLoad();
            metric.cpuUsage = processLoad >= 0 ? processLoad * 100 : 0;
            metric.systemCpu = systemLoad >= 0 ? systemLoad * 100 : 0;
        } catch (Throwable t) {
            // 部分 JVM 不支持 getProcessCpuLoad，置 0 忽略
            metric.cpuUsage = 0;
            metric.systemCpu = 0;
        }
    }
}
