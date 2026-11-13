"""capacity 移植等价性测试：逐条对应 CapacityAnalyzerScienceTest 的 14 个用例。"""

import math

from capacity.analyzer import analyze
from capacity.curve_fitter import fit_quadratic, knee_concurrency, p95_interval, qps_interval
from engine.result import StagePoint


def parabola_xs():
    return [5, 10, 15, 20, 25, 30]


def parabola_qps():
    return [-0.5 * x * x + 20 * x for x in parabola_xs()]


def knee_rt():
    # RT 曲线：20 并发前平缓，之后陡升
    return [50, 52, 55, 60, 130, 320]


def stages(rt, error_rate_of_last):
    xs, qs = parabola_xs(), parabola_qps()
    points = []
    for i, x in enumerate(xs):
        points.append(
            StagePoint(
                concurrency=int(x),
                qps=qs[i],
                p50_ms=rt[i] * 0.4,
                p95_ms=rt[i],
                p99_ms=rt[i] * 1.5,
                error_rate=error_rate_of_last if i == len(xs) - 1 else 0.001,
                total_requests=int(qs[i] * 10),  # 每档 10s × QPS 个请求
            )
        )
    return points


# ---------------- 二项式拟合 ----------------

def test_fit_quadratic_parabola_exact():
    fit = fit_quadratic(parabola_xs(), parabola_qps())
    assert fit is not None
    assert fit.a == pytest_approx(-0.5, 1e-6)
    assert fit.b == pytest_approx(20.0, 1e-6)
    assert fit.k == pytest_approx(0.0, 1e-6)
    assert fit.r2 == pytest_approx(1.0, 1e-9)
    assert fit.has_vertex
    assert fit.vertex_concurrency == pytest_approx(20.0, 1e-6)
    assert fit.vertex_qps == pytest_approx(200.0, 1e-6)
    assert fit.qps_at(20.0) == pytest_approx(200.0, 1e-6)


def pytest_approx(expected, tol):
    class _A:
        def __eq__(self, actual):
            return abs(actual - expected) <= tol

    return _A()


def test_fit_quadratic_noisy_still_high_r2():
    noise = [0.97, 1.03, 0.98, 1.02, 0.96, 1.01]
    ys = [q * n for q, n in zip(parabola_qps(), noise, strict=False)]
    fit = fit_quadratic(parabola_xs(), ys)
    assert fit is not None
    assert fit.r2 > 0.9, f"带噪声拟合 R² 仍应 > 0.9，实际 {fit.r2}"
    assert fit.has_vertex
    assert abs(fit.vertex_concurrency - 20.0) <= 1.5


def test_fit_quadratic_insufficient_points():
    assert fit_quadratic([10, 20], [100, 150]) is None  # 少于 3 点
    assert fit_quadratic([10, 10, 10], [100, 110, 120]) is None  # 奇异矩阵


# ---------------- 拐点检测 ----------------

def test_knee_concurrency_detects_inflection():
    knee = knee_concurrency(parabola_xs(), knee_rt())
    assert not math.isnan(knee)
    assert 20 <= knee <= 25, f"拐点应落在陡升段（20~25），实际 {knee}"


def test_knee_concurrency_linear_no_knee():
    assert math.isnan(knee_concurrency(parabola_xs(), [50, 60, 70, 80, 90, 100]))


def test_knee_concurrency_too_few_points():
    assert math.isnan(knee_concurrency([10, 20], [50, 60]))


# ---------------- 置信区间 ----------------

def test_qps_interval_narrows_with_samples():
    small = qps_interval(300, 10)
    large = qps_interval(300, 100)
    assert small[0] < 300 < small[1]
    assert large[0] < 300 < large[1]
    assert large[1] - large[0] < small[1] - small[0]
    assert small[0] == pytest_approx(300 - 1.96 * math.sqrt(300) / math.sqrt(10), 1e-6)


def test_p95_interval_contains_point_estimate():
    ci = p95_interval(50, 135, 200, 10000)
    assert ci[0] < 135 < ci[1]
    assert ci[1] - ci[0] < 20
    # σ 由 P99-P95 反推：(200-135)/0.6814 → 半宽 ≈ 1.87
    assert ci[0] == pytest_approx(135 - 1.96 * ((200 - 135) / 0.6814) / math.sqrt(10000), 1e-6)


def test_intervals_degenerate_without_samples():
    assert qps_interval(300, 0) == (300, 300)
    assert p95_interval(0, 0, 0, 0) == (0, 0)


# ---------------- 分析器集成 ----------------

def test_analyze_fills_model_without_changing_verdict():
    result = analyze(stages([50, 60, 70, 80, 90, 100], 0.001), 200)
    # 既有插值口径不变：全程未达阈值 → 外推，取末档
    assert result.extrapolated is True
    assert result.max_concurrency == 30
    assert result.max_qps == 150
    # 科学化字段
    assert result.model is not None
    assert result.model.type == "quadratic"
    assert abs(result.model.a - (-0.5)) <= 1e-3
    assert abs(result.model.b - 20.0) <= 1e-2
    assert abs(result.r2 - 1.0) <= 1e-6
    assert abs(result.model.vertex_concurrency - 20.0) <= 1e-2
    assert abs(result.model.vertex_qps - 200.0) <= 1e-2
    assert result.model.points == 6
    # 拟合曲线：24 点等距重采样
    assert len(result.fitted_curve) == 24
    assert abs(result.fitted_curve[0].concurrency - 5.0) <= 1e-6
    assert abs(result.fitted_curve[23].concurrency - 30.0) <= 1e-6
    # 置信区间
    assert len(result.confidence_interval) == 6
    ci = result.confidence_interval[0]
    assert abs(ci.qps_samples - 10.0) <= 1e-6
    assert ci.qps_lower < ci.qps < ci.qps_upper
    assert ci.p95_lower < ci.p95_ms < ci.p95_upper


def test_analyze_detects_rt_knee_and_interpolates():
    result = analyze(stages(knee_rt(), 0.001), 200)
    # P95 在 25(130) 与 30(320) 之间越过阈值 → 插值交点 ≈ 26.8
    assert result.extrapolated is False
    assert result.max_concurrency == 26
    assert result.max_qps == 173
    assert 20 <= result.rt_knee_concurrency <= 25


def test_analyze_skips_overloaded_stage_in_fit():
    result = analyze(stages(knee_rt(), 0.5), 200)
    # 末档错误率 50% 被截断，拟合仅用前 5 档
    assert result.model is not None
    assert result.model.points == 5
    assert len(result.confidence_interval) == 5


def test_analyze_no_model_when_too_few_stages():
    two = stages([50, 60, 70, 80, 90, 100], 0.001)[:2]
    result = analyze(two, 200)
    assert result.model is None
    assert result.r2 == 0
    assert result.fitted_curve == []
    assert result.max_concurrency == 10  # 插值判定不受影响


# ---------------- 向后兼容 ----------------

def test_result_json_backward_compatible():
    result = analyze(stages(knee_rt(), 0.001), 200)
    j = result.to_java_json()
    for key in (
        "maxConcurrency",
        "maxQps",
        "targetP95Ms",
        "extrapolated",
        "curve",
        "model",
        "r2",
        "fittedCurve",
        "confidenceInterval",
        "rtKneeConcurrency",
    ):
        assert key in j, f"缺少契约字段 {key}"
    # 老格式数据（无新字段）仍可解析
    from capacity.analyzer import CapacityResult

    legacy = CapacityResult.model_validate(
        {"maxConcurrency": 30, "maxQps": 150, "targetP95Ms": 200, "extrapolated": True, "curve": []}
    )
    assert legacy.max_concurrency == 30
    assert legacy.max_qps == 150
    assert legacy.model is None
    assert legacy.fitted_curve == []
