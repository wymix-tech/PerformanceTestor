package com.perfserver.capacity;

import com.perfserver.stress.StressTaskResult;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * 容量分析器：基于压测分档采样点（并发-QPS-P95 RT）分段线性插值，
 * 求目标 RT 阈值下可承受的最大并发与 QPS；并在插值之上补充科学化模型
 * （二项式拟合 + R² + 拐点检测 + 95% 置信区间）。
 *
 * <p>算法：按并发升序扫描，找到 P95 RT 首次越过目标阈值的相邻两点，
 * 线性插值出阈值交点（并发/QPS）；若错误率超限提前截断；若全程未达
 * 阈值则标记 extrapolated（容量未触顶）。
 *
 * <p>拟合相关字段（model/r2/fittedCurve/confidenceInterval/rtKneeConcurrency）
 * 仅作展示与解释性输出，<b>不参与</b>既有插值判定，保证历史报告口径兼容。
 */
@Component
public class CapacityAnalyzer {

    /** 错误率截断阈值（超过该错误率的档位视为已过载，不参与插值） */
    private static final double ERROR_CAP = 0.05;
    /** 拟合曲线重采样点数（供前端叠加趋势线） */
    private static final int FITTED_POINTS = 24;

    /**
     * 分析容量：分段线性插值求最大并发/QPS + 科学化拟合补充。
     *
     * @param points     压测分档采样点（stagePoints）
     * @param targetP95Ms 目标 P95 RT 阈值 ms
     */
    public CapacityResult analyze(List<StressTaskResult.StagePoint> points, double targetP95Ms) {
        CapacityResult result = interpolate(points, targetP95Ms);
        fillScience(result);
        return result;
    }

    /**
     * 分段线性插值（原容量判定逻辑，语义与返回值保持不变）。
     */
    private CapacityResult interpolate(List<StressTaskResult.StagePoint> points, double targetP95Ms) {
        CapacityResult result = new CapacityResult();
        result.setTargetP95Ms(targetP95Ms);
        if (points == null || points.isEmpty()) {
            return result;
        }

        List<StressTaskResult.StagePoint> sorted = new ArrayList<>(points);
        sorted.sort(Comparator.comparingInt(StressTaskResult.StagePoint::getConcurrency));

        // 构建曲线（跳过并发为 0 的点）
        List<CapacityResult.CurvePoint> curve = new ArrayList<>();
        for (StressTaskResult.StagePoint p : sorted) {
            if (p.getConcurrency() <= 0) {
                continue;
            }
            CapacityResult.CurvePoint c = new CapacityResult.CurvePoint();
            c.setConcurrency(p.getConcurrency());
            c.setQps(p.getQps());
            c.setP50Ms(p.getP50Ms());
            c.setP95Ms(p.getP95Ms());
            c.setP99Ms(p.getP99Ms());
            c.setErrorRate(p.getErrorRate());
            c.setTotalRequests(p.getTotalRequests());
            curve.add(c);
        }
        result.setCurve(curve);
        if (curve.isEmpty()) {
            return result;
        }

        // 截断到错误率阈值内（过载档位不可信）
        int effectiveEnd = effectiveEnd(curve);

        double threshold = targetP95Ms;
        // 首档已超阈值：容量即为首档
        if (curve.get(0).getP95Ms() > threshold) {
            result.setMaxConcurrency(curve.get(0).getConcurrency());
            result.setMaxQps((int) curve.get(0).getQps());
            result.setExtrapolated(false);
            return result;
        }

        // 扫描交点
        for (int i = 1; i < effectiveEnd; i++) {
            CapacityResult.CurvePoint prev = curve.get(i - 1);
            CapacityResult.CurvePoint cur = curve.get(i);
            if (cur.getP95Ms() > threshold) {
                // 分段线性插值：求 P95 = threshold 的交点
                double ratio = (threshold - prev.getP95Ms()) / (cur.getP95Ms() - prev.getP95Ms());
                double concurrency = prev.getConcurrency() + ratio * (cur.getConcurrency() - prev.getConcurrency());
                double qps = prev.getQps() + ratio * (cur.getQps() - prev.getQps());
                result.setMaxConcurrency((int) Math.max(1, Math.floor(concurrency)));
                result.setMaxQps((int) Math.max(0, Math.floor(qps)));
                result.setExtrapolated(false);
                return result;
            }
        }

        // 全程未达阈值：容量未触顶，结果为测试上限（有效档位内）
        CapacityResult.CurvePoint last = curve.get(effectiveEnd - 1);
        result.setMaxConcurrency(last.getConcurrency());
        result.setMaxQps((int) last.getQps());
        result.setExtrapolated(true);
        return result;
    }

    /** 有效档位上界（首个错误率超限档位的下标，全部有效时为 size） */
    private int effectiveEnd(List<CapacityResult.CurvePoint> curve) {
        for (int i = 0; i < curve.size(); i++) {
            if (curve.get(i).getErrorRate() > ERROR_CAP) {
                return i == 0 ? 1 : i;
            }
        }
        return curve.size();
    }

    /**
     * 科学化学补充：二项式拟合（QPS-并发）+ R² + 拐点 + RT 二阶导拐点 + 95% 置信区间。
     * 采样档位 &lt; 3 或拟合失败时静默跳过（不影响既有判定结果）。
     */
    private void fillScience(CapacityResult result) {
        List<CapacityResult.CurvePoint> curve = result.getCurve();
        if (curve == null || curve.size() < 3) {
            return;
        }
        int end = effectiveEnd(curve);
        if (end < 3) {
            return;
        }
        double[] xs = new double[end];
        double[] qpsYs = new double[end];
        double[] rtYs = new double[end];
        for (int i = 0; i < end; i++) {
            CapacityResult.CurvePoint p = curve.get(i);
            xs[i] = p.getConcurrency();
            qpsYs[i] = p.getQps();
            rtYs[i] = p.getP95Ms();
        }

        // 1) RT-并发二阶导拐点（独立于拟合，数据足够即可检出）
        double knee = CurveFitter.kneeConcurrency(xs, rtYs);
        result.setRtKneeConcurrency(Double.isNaN(knee) ? 0 : knee);

        // 2) 二项式拟合：qps = a·c² + b·c + k
        CurveFitter.QuadraticFit fit = CurveFitter.fitQuadratic(xs, qpsYs);
        if (fit == null) {
            return;
        }
        CapacityResult.FitModel model = new CapacityResult.FitModel();
        model.setType("quadratic");
        model.setA(round4(fit.getA()));
        model.setB(round4(fit.getB()));
        model.setK(round4(fit.getK()));
        model.setR2(round4(fit.getR2()));
        model.setPoints(fit.getPoints());
        if (fit.hasVertex()) {
            model.setVertexConcurrency(round2(fit.getVertexConcurrency()));
            model.setVertexQps(round2(fit.getVertexQps()));
        }
        model.setFormula(String.format("qps = %.4f·c² + %.4f·c + %.4f", fit.getA(), fit.getB(), fit.getK()));
        result.setModel(model);
        result.setR2(model.getR2());

        // 3) 拟合曲线：实测并发区间等距重采样
        double min = xs[0];
        double max = xs[end - 1];
        List<CapacityResult.FittedPoint> fitted = new ArrayList<>();
        int steps = Math.max(2, FITTED_POINTS);
        for (int i = 0; i < steps; i++) {
            double c = min + (max - min) * i / (steps - 1.0);
            CapacityResult.FittedPoint fp = new CapacityResult.FittedPoint();
            fp.setConcurrency(round2(c));
            fp.setQps(round2(fit.qpsAt(c)));
            fitted.add(fp);
        }
        result.setFittedCurve(fitted);

        // 4) 各档 95% 置信区间
        List<CapacityResult.ConfidenceInterval> cis = new ArrayList<>();
        for (int i = 0; i < end; i++) {
            CapacityResult.CurvePoint p = curve.get(i);
            CapacityResult.ConfidenceInterval ci = new CapacityResult.ConfidenceInterval();
            ci.setConcurrency(p.getConcurrency());
            ci.setQps(p.getQps());
            ci.setP95Ms(p.getP95Ms());
            // QPS 采样点数 = 该档采样秒数（totalRequests / qps）
            double seconds = p.getQps() > 0 ? p.getTotalRequests() / p.getQps() : 0;
            ci.setQpsSamples(round2(Math.max(0, seconds)));
            ci.setRtSamples(p.getTotalRequests());
            double[] qpsCi = CurveFitter.qpsInterval(p.getQps(), seconds);
            ci.setQpsLower(round2(qpsCi[0]));
            ci.setQpsUpper(round2(qpsCi[1]));
            double[] rtCi = CurveFitter.p95Interval(p.getP50Ms(), p.getP95Ms(), p.getP99Ms(), ci.getRtSamples());
            ci.setP95Lower(round2(rtCi[0]));
            ci.setP95Upper(round2(rtCi[1]));
            cis.add(ci);
        }
        result.setConfidenceInterval(cis);
    }

    private double round2(double v) {
        return Math.round(v * 100.0) / 100.0;
    }

    private double round4(double v) {
        return Math.round(v * 10000.0) / 10000.0;
    }
}
