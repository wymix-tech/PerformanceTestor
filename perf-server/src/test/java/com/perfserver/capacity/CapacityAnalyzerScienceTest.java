package com.perfserver.capacity;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.stress.StressTaskResult;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 容量评估科学化单测：二项式拟合（系数/拐点/R²）、RT 二阶导拐点检测、
 * 95% 置信区间，以及结果 JSON 的向后兼容（老字段不变、老结果可反序列化）。
 */
class CapacityAnalyzerScienceTest {

    private final CapacityAnalyzer analyzer = new CapacityAnalyzer();
    private final ObjectMapper mapper = new ObjectMapper();

    // ---------------- 二项式拟合 ----------------

    /** 理论抛物线 qps = -0.5c² + 20c（顶点 c=20, qps=200） */
    private static double[] parabolaXs() {
        return new double[]{5, 10, 15, 20, 25, 30};
    }

    private static double[] parabolaQps() {
        double[] xs = parabolaXs();
        double[] ys = new double[xs.length];
        for (int i = 0; i < xs.length; i++) {
            ys[i] = -0.5 * xs[i] * xs[i] + 20 * xs[i];
        }
        return ys;
    }

    @Test
    void fitQuadratic_parabolaExact() {
        CurveFitter.QuadraticFit fit = CurveFitter.fitQuadratic(parabolaXs(), parabolaQps());
        assertNotNull(fit);
        assertEquals(-0.5, fit.getA(), 1e-6, "二次项系数");
        assertEquals(20.0, fit.getB(), 1e-6, "一次项系数");
        assertEquals(0.0, fit.getK(), 1e-6, "常数项");
        assertEquals(1.0, fit.getR2(), 1e-9, "R² 应≈1");
        assertTrue(fit.hasVertex(), "a<0 应存在顶点");
        assertEquals(20.0, fit.getVertexConcurrency(), 1e-6, "拐点并发 = -b/2a");
        assertEquals(200.0, fit.getVertexQps(), 1e-6, "顶点 QPS");
        assertEquals(200.0, fit.qpsAt(20.0), 1e-6);
    }

    @Test
    void fitQuadratic_noisyStillHighR2() {
        double[] xs = parabolaXs();
        double[] ys = parabolaQps();
        // 人为加 ±3% 噪声
        double[] noise = {0.97, 1.03, 0.98, 1.02, 0.96, 1.01};
        for (int i = 0; i < ys.length; i++) {
            ys[i] *= noise[i];
        }
        CurveFitter.QuadraticFit fit = CurveFitter.fitQuadratic(xs, ys);
        assertNotNull(fit);
        assertTrue(fit.getR2() > 0.9, "带噪声拟合 R² 仍应 > 0.9，实际 " + fit.getR2());
        assertTrue(fit.hasVertex());
        assertEquals(20.0, fit.getVertexConcurrency(), 1.5, "噪声下拐点仍应接近 20");
    }

    @Test
    void fitQuadratic_insufficientPoints() {
        assertNull(CurveFitter.fitQuadratic(new double[]{10, 20}, new double[]{100, 150}), "少于 3 点不拟合");
        assertNull(CurveFitter.fitQuadratic(new double[]{10, 10, 10}, new double[]{100, 110, 120}), "并发全相同（奇异矩阵）不拟合");
    }

    // ---------------- 拐点检测 ----------------

    /** RT 曲线：20 并发前平缓，之后陡升 */
    private static double[] kneeRt() {
        return new double[]{50, 52, 55, 60, 130, 320};
    }

    @Test
    void kneeConcurrency_detectsInflection() {
        double knee = CurveFitter.kneeConcurrency(parabolaXs(), kneeRt());
        assertFalse(Double.isNaN(knee), "应检出上翘拐点");
        assertTrue(knee >= 20 && knee <= 25, "拐点应落在陡升段（20~25），实际 " + knee);
    }

    @Test
    void kneeConcurrency_linearNoKnee() {
        double[] rt = {50, 60, 70, 80, 90, 100};
        assertTrue(Double.isNaN(CurveFitter.kneeConcurrency(parabolaXs(), rt)), "线性无上翘应无拐点");
    }

    @Test
    void kneeConcurrency_tooFewPoints() {
        assertTrue(Double.isNaN(CurveFitter.kneeConcurrency(new double[]{10, 20}, new double[]{50, 60})));
    }

    // ---------------- 置信区间 ----------------

    @Test
    void qpsInterval_narrowsWithSamples() {
        double[] small = CurveFitter.qpsInterval(300, 10);
        double[] large = CurveFitter.qpsInterval(300, 100);
        assertTrue(small[0] < 300 && small[1] > 300, "CI 应包含点估计");
        assertTrue(large[0] < 300 && large[1] > 300);
        double w1 = small[1] - small[0];
        double w2 = large[1] - large[0];
        assertTrue(w2 < w1, "样本量越大区间越窄：" + w2 + " < " + w1);
        // σ=√300≈17.32，n=10 → 半宽 = 1.96*17.32/√10 ≈ 10.7
        assertEquals(300 - 1.96 * Math.sqrt(300) / Math.sqrt(10), small[0], 1e-6);
    }

    @Test
    void p95Interval_containsPointEstimate() {
        double[] ci = CurveFitter.p95Interval(50, 135, 200, 10000);
        assertTrue(ci[0] < 135 && ci[1] > 135, "P95 CI 应包含点估计");
        assertTrue(ci[1] - ci[0] < 20, "万级样本下区间应较窄，实际宽度 " + (ci[1] - ci[0]));
        // σ 由 P99-P95 反推： (200-135)/0.6814 ≈ 95.4 → 半宽 ≈ 1.87
        assertEquals(135 - 1.96 * ((200 - 135) / 0.6814) / Math.sqrt(10000), ci[0], 1e-6);
    }

    @Test
    void intervals_degenerateWithoutSamples() {
        double[] qps = CurveFitter.qpsInterval(300, 0);
        assertEquals(300, qps[0]);
        assertEquals(300, qps[1]);
        double[] p95 = CurveFitter.p95Interval(0, 0, 0, 0);
        assertEquals(0, p95[0]);
    }

    // ---------------- 分析器集成 ----------------

    /** 构造分档采样点：QPS 走抛物线，RT 走指定序列 */
    private static List<StressTaskResult.StagePoint> stages(double[] rt, double errorRateOfLast) {
        double[] xs = parabolaXs();
        double[] qs = parabolaQps();
        List<StressTaskResult.StagePoint> points = new ArrayList<>();
        for (int i = 0; i < xs.length; i++) {
            StressTaskResult.StagePoint p = new StressTaskResult.StagePoint();
            p.setConcurrency((int) xs[i]);
            p.setQps(qs[i]);
            p.setP50Ms(rt[i] * 0.4);
            p.setP95Ms(rt[i]);
            p.setP99Ms(rt[i] * 1.5);
            p.setErrorRate(i == xs.length - 1 ? errorRateOfLast : 0.001);
            // 每档 10s × QPS 个请求
            p.setTotalRequests((long) (qs[i] * 10));
            points.add(p);
        }
        return points;
    }

    @Test
    void analyze_fillsModelWithoutChangingVerdict() {
        double[] rt = {50, 60, 70, 80, 90, 100};
        CapacityResult result = analyzer.analyze(stages(rt, 0.001), 200);

        // 既有插值口径不变：全程未达阈值 → 外推，取末档
        assertTrue(result.isExtrapolated());
        assertEquals(30, result.getMaxConcurrency());
        assertEquals(150, result.getMaxQps());

        // 科学化字段
        assertNotNull(result.getModel());
        assertEquals("quadratic", result.getModel().getType());
        assertEquals(-0.5, result.getModel().getA(), 1e-3);
        assertEquals(20.0, result.getModel().getB(), 1e-2);
        assertEquals(1.0, result.getR2(), 1e-6);
        assertEquals(20.0, result.getModel().getVertexConcurrency(), 1e-2, "理论最大并发");
        assertEquals(200.0, result.getModel().getVertexQps(), 1e-2, "理论峰值 QPS");
        assertEquals(6, result.getModel().getPoints());

        // 拟合曲线：24 点等距重采样，覆盖实测并发区间
        assertEquals(24, result.getFittedCurve().size());
        assertEquals(5.0, result.getFittedCurve().get(0).getConcurrency(), 1e-6);
        assertEquals(30.0, result.getFittedCurve().get(23).getConcurrency(), 1e-6);

        // 置信区间：每档一组，QPS 采样点数 = 10s
        assertEquals(6, result.getConfidenceInterval().size());
        CapacityResult.ConfidenceInterval ci = result.getConfidenceInterval().get(0);
        assertEquals(10.0, ci.getQpsSamples(), 1e-6);
        assertTrue(ci.getQpsLower() < ci.getQps() && ci.getQpsUpper() > ci.getQps());
        assertTrue(ci.getP95Lower() < ci.getP95Ms() && ci.getP95Upper() > ci.getP95Ms());
    }

    @Test
    void analyze_detectsRtKneeAndInterpolates() {
        CapacityResult result = analyzer.analyze(stages(kneeRt(), 0.001), 200);
        // P95 在 25(130) 与 30(320) 之间越过阈值 → 插值交点 ≈ 26.8
        assertFalse(result.isExtrapolated());
        assertEquals(26, result.getMaxConcurrency());
        assertEquals(173, result.getMaxQps());
        assertTrue(result.getRtKneeConcurrency() >= 20 && result.getRtKneeConcurrency() <= 25,
                "RT 拐点并发应在 20~25，实际 " + result.getRtKneeConcurrency());
    }

    @Test
    void analyze_skipsOverloadedStageInFit() {
        CapacityResult result = analyzer.analyze(stages(kneeRt(), 0.5), 200);
        // 末档错误率 50% 被截断，拟合仅用前 5 档
        assertNotNull(result.getModel());
        assertEquals(5, result.getModel().getPoints());
        assertEquals(5, result.getConfidenceInterval().size());
    }

    @Test
    void analyze_noModelWhenTooFewStages() {
        List<StressTaskResult.StagePoint> two = stages(new double[]{50, 60, 70, 80, 90, 100}, 0.001)
                .subList(0, 2);
        CapacityResult result = analyzer.analyze(two, 200);
        assertNull(result.getModel(), "少于 3 档不产出拟合模型");
        assertEquals(0, result.getR2(), 0);
        assertTrue(result.getFittedCurve().isEmpty());
        assertEquals(10, result.getMaxConcurrency(), "不满足拟合条件时插值判定不受影响");
    }

    // ---------------- 向后兼容 ----------------

    @Test
    void resultJson_backwardCompatible() throws Exception {
        CapacityResult result = analyzer.analyze(stages(kneeRt(), 0.001), 200);
        JsonNode json = mapper.readTree(mapper.writeValueAsString(result));

        // 老字段必须存在且语义不变
        assertTrue(json.has("maxConcurrency"));
        assertTrue(json.has("maxQps"));
        assertTrue(json.has("targetP95Ms"));
        assertTrue(json.has("extrapolated"));
        assertTrue(json.has("curve"));
        // 新增字段
        assertTrue(json.has("model"));
        assertTrue(json.has("r2"));
        assertTrue(json.has("fittedCurve"));
        assertTrue(json.has("confidenceInterval"));
        assertTrue(json.has("rtKneeConcurrency"));

        // 老格式 JSON（无新字段）仍可反序列化，新字段取默认值
        String legacy = "{\"maxConcurrency\":30,\"maxQps\":150,\"targetP95Ms\":200,"
                + "\"extrapolated\":true,\"curve\":[]}";
        CapacityResult parsed = mapper.readValue(legacy, CapacityResult.class);
        assertEquals(30, parsed.getMaxConcurrency());
        assertEquals(150, parsed.getMaxQps());
        assertNull(parsed.getModel());
        assertEquals(0, parsed.getR2(), 0);
        assertTrue(parsed.getFittedCurve().isEmpty());
        assertTrue(parsed.getConfidenceInterval().isEmpty());
    }
}
