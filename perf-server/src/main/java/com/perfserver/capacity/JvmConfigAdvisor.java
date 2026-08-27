package com.perfserver.capacity;

import com.perfserver.entity.JvmMetric;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

/**
 * JVM 配置建议器：基于堆使用曲线与 GC 频率给出 -Xmx/-Xms、GC 收集器与关键参数建议。
 */
@Component
public class JvmConfigAdvisor {

    /** 堆峰值占比阈值（超过则建议增大堆） */
    private static final double HEAP_PEAK_RATIO = 0.8;
    /** Full GC 频率阈值（次/小时，超过建议换收集器） */
    private static final double FULL_GC_PER_HOUR = 12;
    /** 堆建议系数（按峰值 × 1.5 取整到 512MB 对齐） */
    private static final double HEAP_MULTIPLIER = 1.5;

    /**
     * 生成 JVM 配置建议。
     *
     * @param metrics 近期 JVM 指标（可为空，空则给默认建议）
     */
    public CapacityResult.JvmAdvice advise(List<JvmMetric> metrics) {
        CapacityResult.JvmAdvice advice = new CapacityResult.JvmAdvice();
        List<String> reasons = new ArrayList<>();

        if (metrics == null || metrics.isEmpty()) {
            advice.setHeapSizeMb(2048);
            advice.setGcCollector("G1");
            advice.setParams(List.of("-Xms2048m", "-Xmx2048m", "-Xss512k",
                    "-XX:+UseG1GC", "-XX:MaxGCPauseMillis=200", "-XX:+HeapDumpOnOutOfMemoryError"));
            advice.setReasons(List.of("暂无 JVM 指标数据，给出通用生产建议（可按实例规格调整）"));
            return advice;
        }

        // 堆使用分析
        double peakHeap = 0;
        double avgHeap = 0;
        double heapMax = 0;
        long totalFullGc = 0;
        long totalFullGcMs = 0;
        long totalYoungGc = 0;
        long totalYoungGcMs = 0;
        for (JvmMetric m : metrics) {
            if (m.getHeapUsedMb() != null) {
                peakHeap = Math.max(peakHeap, m.getHeapUsedMb());
                avgHeap += m.getHeapUsedMb();
            }
            if (m.getHeapMaxMb() != null) {
                heapMax = Math.max(heapMax, m.getHeapMaxMb());
            }
            if (m.getFullGcCount() != null) {
                totalFullGc = Math.max(totalFullGc, m.getFullGcCount());
            }
            if (m.getFullGcMs() != null) {
                totalFullGcMs = Math.max(totalFullGcMs, m.getFullGcMs());
            }
            if (m.getYoungGcCount() != null) {
                totalYoungGc = Math.max(totalYoungGc, m.getYoungGcCount());
            }
            if (m.getYoungGcMs() != null) {
                totalYoungGcMs = Math.max(totalYoungGcMs, m.getYoungGcMs());
            }
        }
        avgHeap /= metrics.size();

        // GC 频率：基于首尾采样差值推算（每小时）
        double hours = 1;
        if (metrics.size() >= 2) {
            long secs = Duration.between(metrics.get(0).getCollectTime(),
                    metrics.get(metrics.size() - 1).getCollectTime()).getSeconds();
            hours = Math.max(0.05, secs / 3600.0);
        }
        double fullGcPerHour = totalFullGc / hours;
        double youngGcPerHour = totalYoungGc / hours;
        double gcPauseRatio = hours > 0 ? (totalFullGcMs + totalYoungGcMs) / 1000.0 / (hours * 3600) * 100 : 0;

        // 堆大小建议
        int suggestedHeap = 2048;
        if (heapMax > 0) {
            double ratio = peakHeap / heapMax;
            if (ratio > HEAP_PEAK_RATIO) {
                suggestedHeap = roundTo512(peakHeap * HEAP_MULTIPLIER);
                reasons.add(String.format("堆峰值 %.0fMB 已达上限 %.0fMB 的 %.0f%%，建议增大堆至 %.0fMB",
                        peakHeap, heapMax, ratio * 100, (double) suggestedHeap));
            } else {
                suggestedHeap = roundTo512(heapMax);
                reasons.add(String.format("堆峰值占比 %.0f%% 健康，维持当前堆上限 %.0fMB",
                        ratio * 100, (double) suggestedHeap));
            }
        } else {
            suggestedHeap = roundTo512(peakHeap * HEAP_MULTIPLIER);
            reasons.add(String.format("依据堆峰值 %.0fMB 建议堆大小 %.0fMB", peakHeap, (double) suggestedHeap));
        }

        // GC 收集器建议
        String collector = "G1";
        if (fullGcPerHour > FULL_GC_PER_HOUR || (suggestedHeap > 16384 && gcPauseRatio > 1)) {
            collector = "ZGC";
            reasons.add(String.format("Full GC %.1f 次/小时 或大堆低延迟需求，建议切换 ZGC（暂停目标短、大堆友好）", fullGcPerHour));
        } else if (gcPauseRatio > 0.5) {
            collector = "G1";
            reasons.add(String.format("GC 暂停占比 %.2f%%，建议调优 G1 暂停目标并控制新生代大小", gcPauseRatio));
        } else {
            reasons.add(String.format("GC 频率适中（Young %.0f 次/h，Full %.1f 次/h），维持 G1 收集器", youngGcPerHour, fullGcPerHour));
        }

        List<String> params = new ArrayList<>();
        params.add("-Xms" + suggestedHeap + "m");
        params.add("-Xmx" + suggestedHeap + "m");
        params.add("-Xss512k");
        params.add("-XX:+Use" + collector);
        params.add(collector.equals("ZGC")
                ? "-XX:MaxGCPauseMillis=50"
                : "-XX:MaxGCPauseMillis=200");
        params.add("-XX:+HeapDumpOnOutOfMemoryError");
        params.add("-XX:HeapDumpPath=/data/logs/");

        advice.setHeapSizeMb(suggestedHeap);
        advice.setGcCollector(collector);
        advice.setParams(params);
        advice.setReasons(reasons);
        return advice;
    }

    private int roundTo512(double mb) {
        int v = (int) Math.ceil(mb / 512.0) * 512;
        return Math.max(v, 512);
    }
}
