"""T4.2 验收：12 类分布采样器（总方案 §7.1 全集）。

- 每类分布统计特性单测（均值/方差/分位数容差）；
- weighted_categorical 的 values/weights、conditional 的 depends_on 参数键名
  与 B3 distribution_planner/derivation 产出严格一致；
- 口径对照：uniform/normal/lognormal/weighted_categorical 与
  derivation.sample_one 同 DistributionFit + 同 rng 逐值一致（勿另起一套）；
- 估计分布（fitted=False）照常可采样；未知类型/缺参显式失败。
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime

import pytest

from ptde.generator import samplers
from ptde.generator.samplers import zipf
from ptde.models.profile import DistributionFit
from ptde.planner import derivation


def fit(dist_type: str, **params) -> DistributionFit:
    return DistributionFit(type=dist_type, params=params, fitted=True)


def estimated(dist_type: str, **params) -> DistributionFit:
    """B3 显式默认分布形态（fitted=False，source=estimated）。"""
    return DistributionFit(type=dist_type, params=params, fitted=False)


def _freq(seq, pred) -> float:
    return sum(1 for v in seq if pred(v)) / len(seq)


# ---------------------------------------------------------------- uniform


def test_uniform_mean_and_bounds():
    rng = random.Random(1)
    values = [samplers.sample(fit("uniform", min=10, max=20), rng) for _ in range(20000)]
    assert all(10 <= v <= 20 for v in values)
    assert sum(values) / len(values) == pytest.approx(15.0, abs=0.2)


def test_uniform_continuous_mode():
    rng = random.Random(2)
    values = [samplers.sample(fit("uniform", min=0.0, max=1.5), rng) for _ in range(5000)]
    assert all(isinstance(v, float) and 0.0 <= v <= 1.5 for v in values)
    assert sum(values) / len(values) == pytest.approx(0.75, abs=0.05)


def test_uniform_missing_params_fails_explicitly():
    with pytest.raises(ValueError, match="min/max"):
        samplers.sample(fit("uniform"), random.Random(1))


# ---------------------------------------------------------------- normal


def test_normal_mean_and_variance():
    rng = random.Random(3)
    values = [samplers.sample(fit("normal", mean=100, sigma=10), rng)
              for _ in range(30000)]
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    assert mean == pytest.approx(100.0, abs=1.0)
    assert var == pytest.approx(100.0, rel=0.15)
    assert all(v >= 0 for v in values), "口径对齐 derivation：非负数值列语义"


def test_normal_std_alias_and_clamps():
    rng = random.Random(4)
    values = [samplers.sample(fit("normal", mean=50, std=5, min=45, max=55), rng)
              for _ in range(2000)]
    assert all(45 <= v <= 55 for v in values)


def test_normal_missing_mean_fails():
    with pytest.raises(ValueError, match="mean"):
        samplers.sample(fit("normal", sigma=1), random.Random(1))


# ---------------------------------------------------------------- lognormal


def test_lognormal_mean_matches_formula():
    rng = random.Random(5)
    mu, sigma = 1.0, 0.5
    values = [samplers.sample(fit("lognormal", mu=mu, sigma=sigma), rng)
              for _ in range(30000)]
    expected = math.exp(mu + sigma ** 2 / 2)
    assert sum(values) / len(values) == pytest.approx(expected, rel=0.05)
    assert all(v > 0 for v in values)


# ---------------------------------------------------------------- zipf / skewed


def test_zipf_rank_mean_matches_closed_form():
    """rank 均值 ≈ H(N, s-1) / H(N, s)（离散 Zipf 闭式期望）。"""
    n, s = 200, 2.0
    h1 = sum(1.0 / k for k in range(1, n + 1))
    h2 = sum(1.0 / (k * k) for k in range(1, n + 1))
    rng = random.Random(6)
    ranks = [zipf.sample_rank(rng, s, n) for _ in range(20000)]
    assert sum(ranks) / len(ranks) == pytest.approx(h1 / h2, abs=0.4)
    assert min(ranks) >= 1 and max(ranks) <= n


def test_zipf_hotspot_dominates():
    """高倾斜：rank=1 频率显著占优（热点语义）。"""
    rng = random.Random(7)
    ranks = [zipf.sample_rank(rng, 2.5, 1000) for _ in range(20000)]
    assert _freq(ranks, lambda r: r == 1) > 0.7


def test_zipf_skewed_alias_and_collector_shape():
    """B2 collector 形态：type=skewed + fitted_parameter（Zipf 指数）。"""
    a = [samplers.sample(fit("skewed", fitted_model="zipf",
                             fitted_parameter=2.0, max_rank=100), random.Random(8))
         for _ in range(500)]
    b = [samplers.sample(fit("zipf", exponent=2.0, max_rank=100), random.Random(8))
         for _ in range(500)]
    assert a == b


def test_zipf_mean_param_expectation_alignment():
    """params.mean 存在且无值域：直接返回 mean（对齐 distribution_expectation 口径）。"""
    assert samplers.sample(fit("zipf", mean=12.0), random.Random(9)) == 12.0


def test_zipf_values_mapping():
    rng = random.Random(10)
    values_fit = fit("zipf", exponent=3.0, max_rank=100,
                     values=["hot", "warm", "cold"])
    picked = [samplers.sample(values_fit, rng) for _ in range(2000)]
    assert set(picked) <= {"hot", "warm", "cold"}
    assert _freq(picked, lambda v: v == "hot") > _freq(picked, lambda v: v == "cold")


# ---------------------------------------------------------------- weighted_categorical


def test_weighted_categorical_values_weights_contract():
    """values/weights 键名与 B3 distribution_planner 产出严格一致；比例容差内。"""
    rng = random.Random(11)
    f = fit("weighted_categorical", values=["PAID", "PENDING", "DONE"],
            weights=[0.8, 0.15, 0.05])
    picked = [samplers.sample(f, rng) for _ in range(20000)]
    assert _freq(picked, lambda v: v == "PAID") == pytest.approx(0.8, abs=0.02)
    assert _freq(picked, lambda v: v == "PENDING") == pytest.approx(0.15, abs=0.02)
    assert _freq(picked, lambda v: v == "DONE") == pytest.approx(0.05, abs=0.01)


def test_weighted_categorical_mean_matches_expectation_formula():
    """数值类别均值 = Σ 值×权重（derivation.distribution_expectation 公式）。"""
    rng = random.Random(12)
    f = fit("weighted_categorical", values=[10, 20, 30], weights=[0.5, 0.3, 0.2])
    picked = [samplers.sample(f, rng) for _ in range(20000)]
    assert sum(picked) / len(picked) == pytest.approx(17.0, abs=0.3)


def test_weighted_categorical_zero_weight_never_selected():
    rng = random.Random(13)
    f = fit("weighted_categorical", values=["A", "B", "C"], weights=[0.5, 0.0, 0.5])
    assert "B" not in [samplers.sample(f, rng) for _ in range(5000)]


def test_weighted_categorical_dict_shape_compat():
    """兼容 B2 collector 的 {bucket: ratio} 字典形态。"""
    rng = random.Random(14)
    f = DistributionFit(type="weighted_categorical",
                        params={"PAID": 0.7, "PENDING": 0.3}, fitted=True)
    picked = [samplers.sample(f, rng) for _ in range(10000)]
    assert set(picked) == {"PAID", "PENDING"}
    assert _freq(picked, lambda v: v == "PAID") == pytest.approx(0.7, abs=0.03)


def test_weighted_categorical_missing_params_fails():
    with pytest.raises(ValueError, match="weighted_categorical"):
        samplers.sample(fit("weighted_categorical"), random.Random(1))


# ---------------------------------------------------------------- temporal


def test_temporal_bucket_frequencies():
    """按占比选桶、桶内窗口均匀：桶频率与占比容差内（分位数口径）。"""
    now = datetime(2026, 11, 17, tzinfo=UTC)
    rng = random.Random(15)
    f = fit("temporal", buckets={"last_7_days": 0.5, "last_30_days": 0.3,
                                 "last_90_days": 0.15, "older": 0.05})
    sampled = [samplers.sample(f, rng, {"now": now}) for _ in range(6000)]
    assert all((now - v).days >= 0 for v in sampled)

    def bucket_of(v):
        days = (now - v).days
        if days <= 7:
            return "last_7_days"
        if days <= 30:
            return "last_30_days"
        if days <= 90:
            return "last_90_days"
        return "older"

    freqs = {name: _freq(sampled, lambda v, n=name: bucket_of(v) == n)
             for name in ("last_7_days", "last_30_days", "last_90_days", "older")}
    assert freqs["last_7_days"] == pytest.approx(0.5, abs=0.03)
    assert freqs["last_30_days"] == pytest.approx(0.3, abs=0.03)
    assert freqs["last_90_days"] == pytest.approx(0.15, abs=0.02)
    assert freqs["older"] == pytest.approx(0.05, abs=0.02)


def test_temporal_start_end_range():
    rng = random.Random(16)
    f = fit("temporal", start="2026-01-01T00:00:00", end="2026-01-31T00:00:00")
    sampled = [samplers.sample(f, rng) for _ in range(1000)]
    lo = datetime(2026, 1, 1)
    hi = datetime(2026, 1, 31)
    assert all(lo <= v <= hi for v in sampled)


def test_temporal_unknown_bucket_fails():
    with pytest.raises(ValueError, match="桶名"):
        samplers.sample(fit("temporal", buckets={"weird": 1.0}), random.Random(1))


# ---------------------------------------------------------------- sequential


def test_sequential_matches_primitive_seq_semantics():
    """step=1 时与 primitive.seq_value / legacy SeqRule 语义逐值一致。"""
    from datagen.model import FieldRule
    from ptde.generator import primitive

    rule = FieldRule(type=FieldRule.TYPE_SEQ, seq_prefix="ORD_", seq_start=7)
    f = fit("sequential", seq_start=7, seq_prefix="ORD_")
    rng = random.Random(17)
    for i in range(100):
        assert samplers.sample(f, rng, {"row_index": i}) == primitive.seq_value(rule, i)


def test_sequential_step_and_defaults():
    rng = random.Random(18)
    f = fit("sequential", seq_start=10, step=5)
    values = [samplers.sample(f, rng, {"row_index": i}) for i in range(5)]
    assert values == ["10", "15", "20", "25", "30"]
    assert samplers.sample(fit("sequential"), random.Random(1), {"row_index": 3}) == "4"


# ---------------------------------------------------------------- random 离散值


def test_random_discrete_uniform_frequency():
    rng = random.Random(19)
    f = fit("random", values=["A", "B", "C", "D"])
    picked = [samplers.sample(f, rng) for _ in range(20000)]
    assert set(picked) == {"A", "B", "C", "D"}
    for v in ("A", "B", "C", "D"):
        assert _freq(picked, lambda x, t=v: x == t) == pytest.approx(0.25, abs=0.02)


def test_random_missing_values_fails():
    with pytest.raises(ValueError, match="values"):
        samplers.sample(fit("random"), random.Random(1))


# ---------------------------------------------------------------- conditional


def _paid_at_branches():
    return {
        "PAID": {"type": "temporal",
                 "params": {"start": "2026-01-01T00:00:00",
                            "end": "2026-01-31T00:00:00"}},
        "PENDING": None,
        "*": "unknown",
    }


def test_conditional_depends_on_key_contract():
    """depends_on 参数键名与 B3 planner.py 折叠形态严格一致。"""
    f_b3 = DistributionFit(type="conditional",
                           params={"depends_on": ["status"], "basis": "heuristic"},
                           fitted=False)
    assert samplers.sample(
        DistributionFit(type="conditional",
                        params={"depends_on": ["status"],
                                "branches": {"PAID": 1, "*": 0}}),
        random.Random(20), {"status": "PAID"},
    ) == 1
    # 原 B3 形态（无 branches）显式失败——不得静默退化为边缘分布
    with pytest.raises(ValueError, match="branches"):
        samplers.sample(f_b3, random.Random(20), {"status": "PAID"})


def test_conditional_branch_dispatch_and_fallback():
    rng = random.Random(21)
    f = DistributionFit(type="conditional", params={
        "depends_on": ["status"], "branches": _paid_at_branches()}, fitted=True)
    sampled = [samplers.sample(f, rng, {"status": "PAID"}) for _ in range(500)]
    lo = datetime(2026, 1, 1)
    hi = datetime(2026, 1, 31)
    assert all(lo <= v <= hi for v in sampled)
    assert samplers.sample(f, random.Random(1), {"status": "PENDING"}) is None
    assert samplers.sample(f, random.Random(1), {"status": "REFUNDED"}) == "unknown"


def test_conditional_unmatched_key_fails_explicitly():
    f = DistributionFit(type="conditional", params={
        "depends_on": ["status"], "branches": {"PAID": 1}}, fitted=True)
    with pytest.raises(ValueError, match="无匹配分支"):
        samplers.sample(f, random.Random(1), {"status": "PENDING"})


def test_conditional_multi_depends_and_list_branch():
    f = DistributionFit(type="conditional", params={
        "depends_on": ["region", "vip"],
        "branches": {("cn", True): ["FAST", "EXPRESS"], "*": "STD"},
    }, fitted=True)
    assert samplers.sample(f, random.Random(1), {"region": "cn", "vip": True}) \
        in ("FAST", "EXPRESS")
    assert samplers.sample(f, random.Random(1), {"region": "us", "vip": False}) == "STD"


# ---------------------------------------------------------------- correlated


def test_correlated_pearson_matches_rho():
    """Pearson 相关系数 ≈ rho（联合分布语义，容差 ±0.06）。"""
    rng = random.Random(22)
    f = fit("correlated", depends_on="quantity", correlation=0.8,
            mean=100.0, sigma=20.0, partner_mean=50.0, partner_sigma=10.0)
    xs, ys = [], []
    for _ in range(8000):
        x = rng.gauss(50.0, 10.0)
        ys.append(samplers.sample(f, rng, {"quantity": x}))
        xs.append(x)
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True))
    sx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    sy = math.sqrt(sum((b - my) ** 2 for b in ys))
    assert cov / (sx * sy) == pytest.approx(0.8, abs=0.06)


def test_correlated_missing_partner_fails():
    with pytest.raises(ValueError, match="关联字段"):
        samplers.sample(fit("correlated", depends_on="quantity"), random.Random(1))


# ---------------------------------------------------------------- null_ratio


def test_null_ratio_frequency():
    rng = random.Random(23)
    f = fit("null_ratio", null_ratio=0.3,
            base={"type": "uniform", "params": {"min": 1, "max": 10}})
    picked = [samplers.sample(f, rng) for _ in range(20000)]
    non_null = [v for v in picked if v is not None]
    assert _freq(picked, lambda v: v is None) == pytest.approx(0.3, abs=0.02)
    assert all(1 <= v <= 10 for v in non_null)


def test_null_ratio_bounds():
    f = fit("null_ratio", null_ratio=1.0,
            base={"type": "uniform", "params": {"min": 1, "max": 10}})
    assert samplers.sample(f, random.Random(1)) is None
    f0 = fit("null_ratio", null_ratio=0.0,
             base={"type": "uniform", "params": {"min": 1, "max": 10}})
    # null 检查先消耗一次 random：对照流需同步消耗一次
    r = random.Random(1)
    r.random()
    assert samplers.sample(f0, random.Random(1)) == samplers.sample(
        fit("uniform", min=1, max=10), r)


def test_null_ratio_missing_params_fails():
    with pytest.raises(ValueError, match="null_ratio"):
        samplers.sample(fit("null_ratio"), random.Random(1))
    with pytest.raises(ValueError, match="base"):
        rng = random.Random(1)
        for _ in range(20):
            samplers.sample(fit("null_ratio", null_ratio=0.5), rng)


# ---------------------------------------------------------------- length


def test_length_uniform_mean_and_bounds():
    rng = random.Random(24)
    f = fit("length", min_length=5, max_length=25)
    lengths = [samplers.sample(f, rng) for _ in range(10000)]
    assert all(5 <= v <= 25 for v in lengths)
    assert sum(lengths) / len(lengths) == pytest.approx(15.0, abs=0.3)


def test_length_mean_mode():
    rng = random.Random(25)
    f = fit("length", min_length=10, max_length=50, mean=30)
    lengths = [samplers.sample(f, rng) for _ in range(8000)]
    assert all(10 <= v <= 50 for v in lengths)
    assert sum(lengths) / len(lengths) == pytest.approx(30.0, abs=1.0)


def test_length_b3_default_shape_via_dispatcher():
    """B3 字符列默认形态：uniform + min_length/max_length → 长度语义。"""
    rng = random.Random(26)
    f = estimated("uniform", min_length=1, max_length=64)
    assert all(1 <= v <= 64 for v in
               [samplers.sample(f, rng) for _ in range(2000)])


# ---------------------------------------------------------------- 分发与口径对照


def test_supported_types_cover_section_7_1():
    """覆盖总方案 §7.1 全部 12 类分布。"""
    assert set(samplers.SUPPORTED_TYPES) == {
        "uniform", "normal", "lognormal", "zipf", "skewed",
        "weighted_categorical", "temporal", "sequential", "random",
        "conditional", "correlated", "null_ratio", "length",
    }
    assert samplers.sample(DistributionFit(
        type="weightedcategorical", params={"values": [1], "weights": [1.0]}),
        random.Random(1)) == 1


def test_unsupported_type_fails_explicitly():
    with pytest.raises(ValueError, match="不支持的分布类型"):
        samplers.sample(fit("mystery"), random.Random(1))


def test_sample_params_convenience():
    assert samplers.sample_params("uniform", {"min": 2, "max": 2},
                                  random.Random(1)) == 2


@pytest.mark.parametrize("dist_type,params", [
    ("uniform", {"min": 1, "max": 100}),
    ("normal", {"mean": 50, "sigma": 5}),
    ("lognormal", {"mu": 0.5, "sigma": 0.3}),
    ("weighted_categorical", {"values": [10, 20, 30], "weights": [0.5, 0.3, 0.2]}),
])
def test_expectation_alignment_with_derivation(dist_type, params):
    """口径对照：同 DistributionFit + 同 rng，采样器与 derivation.sample_one
    逐值一致（uniform/normal/lognormal/weighted_categorical 共享形态）。"""
    f = fit(dist_type, **params)
    a = [samplers.sample(f, random.Random(42)) for _ in range(500)]
    b = [derivation.sample_one(f, random.Random(42), fallback=0.0)
         for _ in range(500)]
    assert a == b


@pytest.mark.parametrize("dist_type,params", [
    ("uniform", {"min": 1, "max": 1000}),
    ("weighted_categorical", {"values": [1, 2], "weights": [0.9, 0.1]}),
])
def test_estimated_fit_is_sampleable(dist_type, params):
    """估计分布（fitted=False / source=estimated）照常可采样（不伪装统计口径）。"""
    values = [samplers.sample(estimated(dist_type, **params), random.Random(42))
              for _ in range(100)]
    assert all(v is not None for v in values)
