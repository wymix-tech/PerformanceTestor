package com.perfserver.capacity;

import com.perfserver.entity.JvmMetric;
import com.perfserver.stress.StressTaskResult;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;

/**
 * 扩容触发规则引擎：综合压测结果与近期 JVM 指标，评估是否触发扩容及方式。
 *
 * <p>触发条件：CPU 持续高负载 / RT P99 超阈值 / QPS 接近容量上限 /
 * 线程数高位 / Full GC 频繁 / 错误率超限。
 */
@Component
public class ScaleAdviceEngine {

    private final CapacityProperties props;

    public ScaleAdviceEngine(CapacityProperties props) {
        this.props = props;
    }

    /**
     * 评估扩容建议。
     *
     * @param capacity  容量评估结果（含最大并发/QPS）
     * @param metrics   近期 JVM 指标（可空）
     * @param taskResult 压测结果（可空）
     */
    public List<CapacityResult.ScaleAdvice> evaluate(CapacityResult capacity,
                                                     List<JvmMetric> metrics,
                                                     StressTaskResult taskResult) {
        List<CapacityResult.ScaleAdvice> advices = new ArrayList<>();

        // 1. CPU 持续高负载
        if (metrics != null && !metrics.isEmpty()) {
            double avgCpu = metrics.stream().mapToDouble(m ->
                    m.getCpuUsage() == null ? 0 : m.getCpuUsage()).average().orElse(0);
            if (avgCpu > props.getCpuThreshold()) {
                advices.add(advice("cpu", "high",
                        String.format("近期进程 CPU 平均 %.1f%% 超过阈值 %.0f%%，存在持续高负载", avgCpu, props.getCpuThreshold()),
                        "scale-out", "提升实例数分摊 CPU 负载"));
            }
        }

        // 2. RT P99 超阈值
        if (taskResult != null && taskResult.getP99Ms() > 0
                && taskResult.getP99Ms() > props.getTargetP95Ms() * 2) {
            advices.add(advice("rt_p99", "high",
                    String.format("压测 P99 RT %.0fms 远超目标阈值 %.0fms，响应延迟劣化", taskResult.getP99Ms(), props.getTargetP95Ms()),
                    "scale-out", "增加实例或优化热点路径"));
        }

        // 3. QPS 接近容量上限
        if (capacity != null && capacity.getMaxQps() > 0 && taskResult != null) {
            double usage = taskResult.getQps() * 100.0 / capacity.getMaxQps();
            if (usage >= 90) {
                int instances = (int) Math.ceil(usage / 100.0) + 1;
                advices.add(advice("qps_saturation", "high",
                        String.format("当前 QPS %.0f 已达容量上限 %.0f 的 %.0f%%，接近饱和", taskResult.getQps(), (double) capacity.getMaxQps(), usage),
                        "scale-out", "建议扩容至 " + instances + " 个实例以预留 50% 余量"));
            } else if (usage >= 70) {
                advices.add(advice("qps_saturation", "medium",
                        String.format("当前 QPS %.0f 为容量上限 %.0f 的 %.0f%%，建议关注增长趋势", taskResult.getQps(), (double) capacity.getMaxQps(), usage),
                        "scale-out", "可提前扩容 1 个实例"));
            }
        }

        // 4. 线程数高位（近似线程池饱和）
        if (metrics != null && metrics.size() >= 3) {
            double avgThreads = metrics.stream().mapToDouble(m ->
                    m.getThreadCount() == null ? 0 : m.getThreadCount()).average().orElse(0);
            if (avgThreads > 500) {
                advices.add(advice("thread_pool", "medium",
                        String.format("平均线程数 %.0f 处于高位，存在线程池资源紧张风险", avgThreads),
                        "scale-up", "提升单实例线程池容量或垂直扩容"));
            }
        }

        // 5. Full GC 频繁
        if (metrics != null && metrics.size() >= 2) {
            JvmMetric first = metrics.get(0);
            JvmMetric last = metrics.get(metrics.size() - 1);
            long gcDelta = last.getFullGcCount() - first.getFullGcCount();
            double minutes = java.time.Duration.between(first.getCollectTime(), last.getCollectTime())
                    .toMinutes();
            if (minutes >= 1) {
                double perMinute = gcDelta / minutes;
                if (perMinute > props.getFullGcThreshold()) {
                    advices.add(advice("full_gc", "high",
                            String.format("Full GC 频率 %.1f 次/分钟，超过阈值 %d 次/分钟，可能触发停顿", perMinute, props.getFullGcThreshold()),
                            "scale-up", "增大堆内存或切换 GC 收集器"));
                }
            }
        }

        // 6. 错误率超限
        if (taskResult != null && taskResult.getErrorRate() > props.getErrorRateThreshold() * 100) {
            advices.add(advice("error_rate", "high",
                    String.format("压测错误率 %.2f%% 超过阈值 %.0f%%", taskResult.getErrorRate(), props.getErrorRateThreshold() * 100),
                    "scale-out", "服务过载或异常，建议扩容并排查错误"));
        }

        return advices;
    }

    private CapacityResult.ScaleAdvice advice(String triggerType, String severity,
                                              String description, String action, String detail) {
        CapacityResult.ScaleAdvice a = new CapacityResult.ScaleAdvice();
        a.setTriggerType(triggerType);
        a.setSeverity(severity);
        a.setDescription(description);
        a.setAction(action);
        a.setSuggestDetail(detail);
        return a;
    }
}
