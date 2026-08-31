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
    /**
     * 拟合模型（二项式 {@code qps = a·c² + b·c + k}）；采样档位 &lt; 3 时为 null。
     * 向后兼容新增字段，不参与既有插值判定。
     */
    private FitModel model;
    /** 拟合优度 R²（与 model.r2 同值，冗余字段便于前端直接取用） */
    private double r2;
    /** 拟合曲线（等距重采样，供前端叠加趋势线） */
    private List<FittedPoint> fittedCurve = new ArrayList<>();
    /** 各采样档位的 95% 置信区间 */
    private List<ConfidenceInterval> confidenceInterval = new ArrayList<>();
    /** RT-并发二阶导拐点并发（0 = 未检出上翘拐点） */
    private double rtKneeConcurrency;
    /** 扩容建议 */
    private List<ScaleAdvice> advices = new ArrayList<>();
    /** JVM 配置建议 */
    private JvmAdvice jvmAdvice;

    /** 曲线采样点 */
    public static class CurvePoint {
        private int concurrency;
        private double qps;
        private double p50Ms;
        private double p95Ms;
        private double p99Ms;
        private double errorRate;
        /** 该档请求数（置信区间样本量；向后兼容新增） */
        private long totalRequests;

        public double getP50Ms() {
            return p50Ms;
        }

        public void setP50Ms(double p50Ms) {
            this.p50Ms = p50Ms;
        }

        public long getTotalRequests() {
            return totalRequests;
        }

        public void setTotalRequests(long totalRequests) {
            this.totalRequests = totalRequests;
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

    /** 拟合模型：二项式 {@code qps = a·c² + b·c + k} */
    public static class FitModel {
        /** 模型类型：quadratic */
        private String type = "quadratic";
        private double a;
        private double b;
        private double k;
        private double r2;
        /** 理论最大并发（抛物线顶点，a&lt;0 时有效；否则 0） */
        private double vertexConcurrency;
        /** 理论峰值 QPS（顶点处） */
        private double vertexQps;
        /** 参与拟合的采样点数 */
        private int points;
        /** 模型表达式（前端展示用） */
        private String formula;

        public String getType() {
            return type;
        }

        public void setType(String type) {
            this.type = type;
        }

        public double getA() {
            return a;
        }

        public void setA(double a) {
            this.a = a;
        }

        public double getB() {
            return b;
        }

        public void setB(double b) {
            this.b = b;
        }

        public double getK() {
            return k;
        }

        public void setK(double k) {
            this.k = k;
        }

        public double getR2() {
            return r2;
        }

        public void setR2(double r2) {
            this.r2 = r2;
        }

        public double getVertexConcurrency() {
            return vertexConcurrency;
        }

        public void setVertexConcurrency(double vertexConcurrency) {
            this.vertexConcurrency = vertexConcurrency;
        }

        public double getVertexQps() {
            return vertexQps;
        }

        public void setVertexQps(double vertexQps) {
            this.vertexQps = vertexQps;
        }

        public int getPoints() {
            return points;
        }

        public void setPoints(int points) {
            this.points = points;
        }

        public String getFormula() {
            return formula;
        }

        public void setFormula(String formula) {
            this.formula = formula;
        }
    }

    /** 拟合曲线采样点 */
    public static class FittedPoint {
        private double concurrency;
        private double qps;

        public double getConcurrency() {
            return concurrency;
        }

        public void setConcurrency(double concurrency) {
            this.concurrency = concurrency;
        }

        public double getQps() {
            return qps;
        }

        public void setQps(double qps) {
            this.qps = qps;
        }
    }

    /** 单档 95% 置信区间（QPS 与 P95 各一组上下界） */
    public static class ConfidenceInterval {
        private int concurrency;
        /** 该档采样点数（QPS 用采样秒数） */
        private double qpsSamples;
        /** 该档请求数（P95 用） */
        private long rtSamples;
        private double qps;
        private double qpsLower;
        private double qpsUpper;
        private double p95Ms;
        private double p95Lower;
        private double p95Upper;

        public int getConcurrency() {
            return concurrency;
        }

        public void setConcurrency(int concurrency) {
            this.concurrency = concurrency;
        }

        public double getQpsSamples() {
            return qpsSamples;
        }

        public void setQpsSamples(double qpsSamples) {
            this.qpsSamples = qpsSamples;
        }

        public long getRtSamples() {
            return rtSamples;
        }

        public void setRtSamples(long rtSamples) {
            this.rtSamples = rtSamples;
        }

        public double getQps() {
            return qps;
        }

        public void setQps(double qps) {
            this.qps = qps;
        }

        public double getQpsLower() {
            return qpsLower;
        }

        public void setQpsLower(double qpsLower) {
            this.qpsLower = qpsLower;
        }

        public double getQpsUpper() {
            return qpsUpper;
        }

        public void setQpsUpper(double qpsUpper) {
            this.qpsUpper = qpsUpper;
        }

        public double getP95Ms() {
            return p95Ms;
        }

        public void setP95Ms(double p95Ms) {
            this.p95Ms = p95Ms;
        }

        public double getP95Lower() {
            return p95Lower;
        }

        public void setP95Lower(double p95Lower) {
            this.p95Lower = p95Lower;
        }

        public double getP95Upper() {
            return p95Upper;
        }

        public void setP95Upper(double p95Upper) {
            this.p95Upper = p95Upper;
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

    public FitModel getModel() {
        return model;
    }

    public void setModel(FitModel model) {
        this.model = model;
    }

    public double getR2() {
        return r2;
    }

    public void setR2(double r2) {
        this.r2 = r2;
    }

    public List<FittedPoint> getFittedCurve() {
        return fittedCurve;
    }

    public void setFittedCurve(List<FittedPoint> fittedCurve) {
        this.fittedCurve = fittedCurve;
    }

    public List<ConfidenceInterval> getConfidenceInterval() {
        return confidenceInterval;
    }

    public void setConfidenceInterval(List<ConfidenceInterval> confidenceInterval) {
        this.confidenceInterval = confidenceInterval;
    }

    public double getRtKneeConcurrency() {
        return rtKneeConcurrency;
    }

    public void setRtKneeConcurrency(double rtKneeConcurrency) {
        this.rtKneeConcurrency = rtKneeConcurrency;
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
