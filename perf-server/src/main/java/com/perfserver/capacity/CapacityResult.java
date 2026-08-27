package com.perfserver.capacity;

import java.util.ArrayList;
import java.util.List;

/**
 * 容量评估结果：目标 RT 阈值下最大可承受并发/QPS + QPS-RT 曲线 + 扩容建议 + JVM 配置建议。
 *
 * <p>随压测任务 result_json 落库（capacity 字段），供前端展示与 AI 工具返回。
 */
public class CapacityResult {

    /** 目标 RT 阈值下可承受最大并发 */
    private int maxConcurrency;
    /** 对应最大 QPS */
    private int maxQps;
    /** 目标 P95 阈值 ms */
    private double targetP95Ms;
    /** 是否已超出全部测试并发仍未达阈值（容量未触顶，结果为测试上限） */
    private boolean extrapolated;
    /** 曲线采样点（并发-QPS-P95） */
    private List<CurvePoint> curve = new ArrayList<>();
    /** 扩容建议 */
    private List<ScaleAdvice> advices = new ArrayList<>();
    /** JVM 配置建议 */
    private JvmAdvice jvmAdvice;

    /** 曲线采样点 */
    public static class CurvePoint {
        private int concurrency;
        private double qps;
        private double p95Ms;
        private double p99Ms;
        private double errorRate;

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
    }

    /** 扩容建议 */
    public static class ScaleAdvice {
        /** 触发类型：cpu / rt_p99 / qps_saturation / thread_pool / full_gc / error_rate */
        private String triggerType;
        /** 风险等级：high / medium / low */
        private String severity;
        private String description;
        /** 扩容方式：scale-out（水平）/ scale-up（垂直） */
        private String action;
        /** 建议实例数（水平扩容时） */
        private int suggestInstances;
        /** 建议每实例并发/容量参考 */
        private String suggestDetail;

        public String getTriggerType() {
            return triggerType;
        }

        public void setTriggerType(String triggerType) {
            this.triggerType = triggerType;
        }

        public String getSeverity() {
            return severity;
        }

        public void setSeverity(String severity) {
            this.severity = severity;
        }

        public String getDescription() {
            return description;
        }

        public void setDescription(String description) {
            this.description = description;
        }

        public String getAction() {
            return action;
        }

        public void setAction(String action) {
            this.action = action;
        }

        public int getSuggestInstances() {
            return suggestInstances;
        }

        public void setSuggestInstances(int suggestInstances) {
            this.suggestInstances = suggestInstances;
        }

        public String getSuggestDetail() {
            return suggestDetail;
        }

        public void setSuggestDetail(String suggestDetail) {
            this.suggestDetail = suggestDetail;
        }
    }

    /** JVM 配置建议 */
    public static class JvmAdvice {
        /** 建议堆大小 MB */
        private int heapSizeMb;
        /** 建议 GC 收集器 */
        private String gcCollector;
        /** 建议 JVM 参数 */
        private List<String> params = new ArrayList<>();
        /** 建议依据 */
        private List<String> reasons = new ArrayList<>();

        public int getHeapSizeMb() {
            return heapSizeMb;
        }

        public void setHeapSizeMb(int heapSizeMb) {
            this.heapSizeMb = heapSizeMb;
        }

        public String getGcCollector() {
            return gcCollector;
        }

        public void setGcCollector(String gcCollector) {
            this.gcCollector = gcCollector;
        }

        public List<String> getParams() {
            return params;
        }

        public void setParams(List<String> params) {
            this.params = params;
        }

        public List<String> getReasons() {
            return reasons;
        }

        public void setReasons(List<String> reasons) {
            this.reasons = reasons;
        }
    }

    public int getMaxConcurrency() {
        return maxConcurrency;
    }

    public void setMaxConcurrency(int maxConcurrency) {
        this.maxConcurrency = maxConcurrency;
    }

    public int getMaxQps() {
        return maxQps;
    }

    public void setMaxQps(int maxQps) {
        this.maxQps = maxQps;
    }

    public double getTargetP95Ms() {
        return targetP95Ms;
    }

    public void setTargetP95Ms(double targetP95Ms) {
        this.targetP95Ms = targetP95Ms;
    }

    public boolean isExtrapolated() {
        return extrapolated;
    }

    public void setExtrapolated(boolean extrapolated) {
        this.extrapolated = extrapolated;
    }

    public List<CurvePoint> getCurve() {
        return curve;
    }

    public void setCurve(List<CurvePoint> curve) {
        this.curve = curve;
    }

    public List<ScaleAdvice> getAdvices() {
        return advices;
    }

    public void setAdvices(List<ScaleAdvice> advices) {
        this.advices = advices;
    }

    public JvmAdvice getJvmAdvice() {
        return jvmAdvice;
    }

    public void setJvmAdvice(JvmAdvice jvmAdvice) {
        this.jvmAdvice = jvmAdvice;
    }
}
