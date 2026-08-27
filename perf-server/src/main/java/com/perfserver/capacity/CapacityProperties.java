package com.perfserver.capacity;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/**
 * 容量评估与扩容建议阈值配置（perf.capacity.*，见 application.yml）。
 */
@Component
@ConfigurationProperties(prefix = "perf.capacity")
public class CapacityProperties {

    /** 目标 P95 RT 阈值 ms（容量评估与扩容触发共用） */
    private double targetP95Ms = 200;
    /** CPU 使用率触发扩容阈值 % */
    private double cpuThreshold = 80;
    /** 错误率触发阈值（小数，如 0.05 = 5%） */
    private double errorRateThreshold = 0.05;
    /** 线程池活跃占比触发阈值 */
    private double threadPoolUsageThreshold = 0.9;
    /** 每分钟 Full GC 次数触发阈值 */
    private int fullGcThreshold = 5;
    /** 扩容建议分析时间窗口（分钟） */
    private int sampleWindowMin = 30;

    public double getTargetP95Ms() {
        return targetP95Ms;
    }

    public void setTargetP95Ms(double targetP95Ms) {
        this.targetP95Ms = targetP95Ms;
    }

    public double getCpuThreshold() {
        return cpuThreshold;
    }

    public void setCpuThreshold(double cpuThreshold) {
        this.cpuThreshold = cpuThreshold;
    }

    public double getErrorRateThreshold() {
        return errorRateThreshold;
    }

    public void setErrorRateThreshold(double errorRateThreshold) {
        this.errorRateThreshold = errorRateThreshold;
    }

    public double getThreadPoolUsageThreshold() {
        return threadPoolUsageThreshold;
    }

    public void setThreadPoolUsageThreshold(double threadPoolUsageThreshold) {
        this.threadPoolUsageThreshold = threadPoolUsageThreshold;
    }

    public int getFullGcThreshold() {
        return fullGcThreshold;
    }

    public void setFullGcThreshold(int fullGcThreshold) {
        this.fullGcThreshold = fullGcThreshold;
    }

    public int getSampleWindowMin() {
        return sampleWindowMin;
    }

    public void setSampleWindowMin(int sampleWindowMin) {
        this.sampleWindowMin = sampleWindowMin;
    }
}
