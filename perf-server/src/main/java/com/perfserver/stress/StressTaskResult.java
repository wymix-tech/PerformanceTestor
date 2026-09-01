package com.perfserver.stress;

import java.util.ArrayList;
import java.util.List;

/**
 * 压测任务结果：整体统计 + 周期采样（实时图表）+ 分档采样点（容量曲线）。
 */
public class StressTaskResult {

    private long taskId;
    private String engineType;
    private String targetUrl;
    private int concurrency;
    private int durationSec;
    /** 预热时长秒（该期请求不计入统计） */
    private int warmupSec;
    /** 并发爬坡时长秒（0 = 全量并发立即启动） */
    private int rampUpSec;
    /** 总请求数 */
    private long totalRequests;
    /** 错误数 */
    private long errorCount;
    /** 错误率 % */
    private double errorRate;
    /** 整体 QPS */
    private double qps;
    private double p50Ms;
    private double p95Ms;
    private double p99Ms;
    private double maxMs;
    private double avgMs;
    /** 周期采样（实时滚动图表） */
    private List<Sample> samples = new ArrayList<>();
    /** 分档采样点（容量评估曲线） */
    private List<StagePoint> stagePoints = new ArrayList<>();
    /** 分步骤统计（多接口混合场景；单接口模式为空列表） */
    private List<StressStatistician.StepStats> stepStats = new ArrayList<>();

    /** 周期采样点 */
    public static class Sample {
        private long sampleTime;
        private int concurrency;
        private double qps;
        private double p50Ms;
        private double p95Ms;
        private double p99Ms;
        private long errorCount;

        public long getSampleTime() {
            return sampleTime;
        }

        public void setSampleTime(long sampleTime) {
            this.sampleTime = sampleTime;
        }

        public int getConcurrency() {
            return concurrency;
        }

        public void setConcurrency(int concurrency) {
            this.concurrency = concurrency;
        }

        public double getQps() {
            return qps;
        }

        public void setQps(double qps) {
            this.qps = qps;
        }

        public double getP50Ms() {
            return p50Ms;
        }

        public void setP50Ms(double p50Ms) {
            this.p50Ms = p50Ms;
        }

        public double getP95Ms() {
            return p95Ms;
        }

        public void setP95Ms(double p95Ms) {
            this.p95Ms = p95Ms;
        }

        public double getP99Ms() {
            return p99Ms;
        }

        public void setP99Ms(double p99Ms) {
            this.p99Ms = p99Ms;
        }

        public long getErrorCount() {
            return errorCount;
        }

        public void setErrorCount(long errorCount) {
            this.errorCount = errorCount;
        }
    }

    /** 分档采样点（单档统计汇总） */
    public static class StagePoint {
        private int concurrency;
        private double qps;
        private double p50Ms;
        private double p95Ms;
        private double p99Ms;
        private double errorRate;
        private long totalRequests;

        public int getConcurrency() {
            return concurrency;
        }

        public void setConcurrency(int concurrency) {
            this.concurrency = concurrency;
        }

        public double getQps() {
            return qps;
        }

        public void setQps(double qps) {
            this.qps = qps;
        }

        public double getP50Ms() {
            return p50Ms;
        }

        public void setP50Ms(double p50Ms) {
            this.p50Ms = p50Ms;
        }

        public double getP95Ms() {
            return p95Ms;
        }

        public void setP95Ms(double p95Ms) {
            this.p95Ms = p95Ms;
        }

        public double getP99Ms() {
            return p99Ms;
        }

        public void setP99Ms(double p99Ms) {
            this.p99Ms = p99Ms;
        }

        public double getErrorRate() {
            return errorRate;
        }

        public void setErrorRate(double errorRate) {
            this.errorRate = errorRate;
        }

        public long getTotalRequests() {
            return totalRequests;
        }

        public void setTotalRequests(long totalRequests) {
            this.totalRequests = totalRequests;
        }
    }

    public long getTaskId() {
        return taskId;
    }

    public void setTaskId(long taskId) {
        this.taskId = taskId;
    }

    public String getEngineType() {
        return engineType;
    }

    public void setEngineType(String engineType) {
        this.engineType = engineType;
    }

    public String getTargetUrl() {
        return targetUrl;
    }

    public void setTargetUrl(String targetUrl) {
        this.targetUrl = targetUrl;
    }

    public int getConcurrency() {
        return concurrency;
    }

    public void setConcurrency(int concurrency) {
        this.concurrency = concurrency;
    }

    public int getWarmupSec() {
        return warmupSec;
    }

    public void setWarmupSec(int warmupSec) {
        this.warmupSec = warmupSec;
    }

    public int getRampUpSec() {
        return rampUpSec;
    }

    public void setRampUpSec(int rampUpSec) {
        this.rampUpSec = rampUpSec;
    }

    public int getDurationSec() {
        return durationSec;
    }

    public void setDurationSec(int durationSec) {
        this.durationSec = durationSec;
    }

    public long getTotalRequests() {
        return totalRequests;
    }

    public void setTotalRequests(long totalRequests) {
        this.totalRequests = totalRequests;
    }

    public long getErrorCount() {
        return errorCount;
    }

    public void setErrorCount(long errorCount) {
        this.errorCount = errorCount;
    }

    public double getErrorRate() {
        return errorRate;
    }

    public void setErrorRate(double errorRate) {
        this.errorRate = errorRate;
    }

    public double getQps() {
        return qps;
    }

    public void setQps(double qps) {
        this.qps = qps;
    }

    public double getP50Ms() {
        return p50Ms;
    }

    public void setP50Ms(double p50Ms) {
        this.p50Ms = p50Ms;
    }

    public double getP95Ms() {
        return p95Ms;
    }

    public void setP95Ms(double p95Ms) {
        this.p95Ms = p95Ms;
    }

    public double getP99Ms() {
        return p99Ms;
    }

    public void setP99Ms(double p99Ms) {
        this.p99Ms = p99Ms;
    }

    public double getMaxMs() {
        return maxMs;
    }

    public void setMaxMs(double maxMs) {
        this.maxMs = maxMs;
    }

    public double getAvgMs() {
        return avgMs;
    }

    public void setAvgMs(double avgMs) {
        this.avgMs = avgMs;
    }

    public List<Sample> getSamples() {
        return samples;
    }

    public void setSamples(List<Sample> samples) {
        this.samples = samples;
    }

    public List<StagePoint> getStagePoints() {
        return stagePoints;
    }

    public void setStagePoints(List<StagePoint> stagePoints) {
        this.stagePoints = stagePoints;
    }

    public List<StressStatistician.StepStats> getStepStats() {
        return stepStats;
    }

    public void setStepStats(List<StressStatistician.StepStats> stepStats) {
        this.stepStats = stepStats == null ? new ArrayList<>() : stepStats;
    }
}
