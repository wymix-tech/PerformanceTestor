"""T4.6 验收：关系生成器（外键按分布采样父子绑定；M:N 选型；复核锚点）。

- assign_children：分布采样每父子行数（与 samplers.sample 同 fit 同 rng
  逐值一致），zero_child_ratio 口径（B2），总量精确收敛到 n_children；
- 1:1 互不重复绑定（超父行数显式失败）；复合键（tuple）逻辑一致；
- M:N 按规模选型（§7.7：computed/sharded/external/quota）+ 可计算映射
  （纯函数 of i）+ 关系配额（超发显式失败）；
- parent_binding 生成后复核（tolerance 比例容差）；
- 端到端逐行复现（验收门禁 1 的 R4.2 管线级口径）。
"""

from __future__ import annotations

import random

import pytest

from ptde.generator.relations import (
    MnStrategy,
    PairQuota,
    assign_children,
    bind_one_to_one,
    computed_pairs,
    logical_join_key,
    review_binding,
    select_mn_strategy,
)
from ptde.generator.seed import field_rng
from ptde.models.profile import DistributionFit

# ---------------------------------------------------------------- 1:N 分布采样绑定


def _uniform_children() -> DistributionFit:
    return DistributionFit(type="uniform", params={"min": 1, "max": 9})


def test_assign_children_round_robin_without_dist():
    parents = ["p1", "p2", "p3"]
    bindings = assign_children(parents, 7, random.Random(1))
    assert bindings == ["p1", "p2", "p3", "p1", "p2", "p3", "p1"]


def test_assign_children_exact_total_and_membership():
    parents = list(range(200))
    bindings = assign_children(parents, 1000, random.Random(42), _uniform_children())
    assert len(bindings) == 1000
    assert set(bindings) <= set(parents)
    counts = {p: bindings.count(p) for p in parents if bindings.count(p)}
    assert sum(counts.values()) == 1000


def test_assign_children_mean_matches_distribution():
    """每父实体子行数均值 ≈ 分布期望（uniform [1,9] 期望 5）。"""
    parents = list(range(200))
    bindings = assign_children(parents, 1000, random.Random(42), _uniform_children())
    active = [p for p in parents if bindings.count(p) > 0]
    mean = sum(bindings.count(p) for p in active) / len(active)
    assert 4.5 <= mean <= 5.5, f"每父子行数均值应约 5，实测 {mean:.2f}"


def test_assign_children_zero_child_ratio():
    """zero_child_ratio ≈ 30%：零子实体父实体占比符合 B2 采集口径。"""
    parents = list(range(1000))
    bindings = assign_children(
        parents, 3500, random.Random(42), _uniform_children(), zero_child_ratio=0.3
    )
    zero = sum(1 for p in parents if bindings.count(p) == 0)
    ratio = zero / len(parents)
    assert 0.24 <= ratio <= 0.36, f"零子占比应约 30%，实测 {ratio:.1%}"
    assert len(bindings) == 3500


def test_assign_children_deterministic():
    parents = list(range(50))
    a = assign_children(parents, 300, random.Random(7), _uniform_children())
    b = assign_children(parents, 300, random.Random(7), _uniform_children())
    assert a == b
    assert a != assign_children(parents, 300, random.Random(8), _uniform_children())


def test_assign_children_explicit_failures():
    with pytest.raises(ValueError, match="父键池为空"):
        assign_children([], 5, random.Random(1))
    with pytest.raises(ValueError, match="zero_child_ratio"):
        assign_children(list(range(10)), 5, random.Random(1), None, zero_child_ratio=0.2)
    with pytest.raises(ValueError, match=r"\[0, 1\)"):
        assign_children(list(range(10)), 5, random.Random(1),
                        _uniform_children(), zero_child_ratio=1.0)
    bad_dist = DistributionFit(
        type="weighted_categorical", params={"values": ["a", "b"], "weights": [1, 1]}
    )
    with pytest.raises(ValueError, match="非数值"):
        assign_children(list(range(10)), 5, random.Random(1), bad_dist)


def test_assign_children_composite_keys():
    parents = [(1, 100), (2, 101), (3, 102)]
    bindings = assign_children(parents, 8, random.Random(3), _uniform_children())
    assert len(bindings) == 8
    assert set(bindings) <= set(parents)


# ---------------------------------------------------------------- 1:1 绑定


def test_bind_one_to_one_distinct_and_deterministic():
    parents = list(range(100, 120))
    bindings = bind_one_to_one(parents, 15, random.Random(42))
    assert len(bindings) == 15
    assert len(set(bindings)) == 15
    assert set(bindings) <= set(parents)
    assert bindings == bind_one_to_one(parents, 15, random.Random(42))


def test_bind_one_to_one_overflow_fails():
    with pytest.raises(ValueError, match="1:1"):
        bind_one_to_one(list(range(10)), 11, random.Random(1))
    assert bind_one_to_one(list(range(10)), 0, random.Random(1)) == []


# ---------------------------------------------------------------- M:N 选型与映射


def test_select_mn_strategy_priority():
    assert select_mn_strategy(1000) is MnStrategy.COMPUTED
    assert select_mn_strategy(1000, memory_budget_pairs=5000) is MnStrategy.COMPUTED
    assert select_mn_strategy(10_000, memory_budget_pairs=5000, shards=4) is MnStrategy.SHARDED
    assert select_mn_strategy(10_000, memory_budget_pairs=5000,
                              external_store=True) is MnStrategy.EXTERNAL
    assert select_mn_strategy(10_000, memory_budget_pairs=5000) is MnStrategy.QUOTA
    with pytest.raises(ValueError, match="必须为正"):
        select_mn_strategy(0)


def test_computed_pairs_pure_function_of_i():
    pairs1 = list(computed_pairs(7, 5, 100))
    pairs2 = list(computed_pairs(7, 5, 100))
    assert pairs1 == pairs2
    assert len(pairs1) == 100
    assert all(0 <= left < 7 and 0 <= right < 5 for left, right in pairs1)
    # 可计算映射：i 直接推导（不耗 rng）
    assert pairs1[0] == (0, 0) and pairs1[7] == (0, (7 * 2654435761) % 5)


def test_computed_pairs_skew_deterministic():
    s1 = list(computed_pairs(20, 10, 500, random.Random(5), skew_exponent=1.2))
    s2 = list(computed_pairs(20, 10, 500, random.Random(5), skew_exponent=1.2))
    assert s1 == s2
    assert all(0 <= left < 20 and 0 <= right < 10 for left, right in s1)
    hot = sum(1 for left, _ in s1 if left < 5) / len(s1)
    # 闭式口径：P(rank<=5) = H(5,1.2)/H(1000,1.2) ≈ 0.47（均匀散列仅 25%）
    assert hot > 0.4, f"Zipf 热点应集中在低 rank，实测 {hot:.1%}"
    with pytest.raises(ValueError, match="rng 必须注入"):
        list(computed_pairs(20, 10, 5, None, skew_exponent=1.2))


def test_computed_pairs_rejects_invalid_ranges():
    with pytest.raises(ValueError, match="必须为正"):
        list(computed_pairs(0, 5, 10))


def test_pair_quota_exhaust_fails_explicitly():
    quota = PairQuota(10)
    quota.consume(6)
    assert quota.remaining == 4
    quota.consume(4)
    with pytest.raises(ValueError, match="超发"):
        quota.consume(1)
    with pytest.raises(ValueError, match="配额必须非负"):
        PairQuota(-1)


# ---------------------------------------------------------------- 逻辑关联与复核


def test_logical_join_key_deterministic_and_recomputable():
    args = ("orders", "orders-0001", "order_no", 7, 42)
    assert logical_join_key(*args) == logical_join_key(*args)
    # 父子两侧以相同参数重算即得相同 token（无外键关联）
    parent_side = logical_join_key("orders", "s1", "order_no", 3, 42)
    child_side = logical_join_key("orders", "s1", "order_no", 3, 42)
    assert parent_side == child_side
    assert logical_join_key("orders", "s1", "order_no", 4, 42) != parent_side
    assert logical_join_key("orders", "s1", "amount", 3, 42) != parent_side
    token = parent_side.rsplit(":", 1)[-1]
    assert len(token) == 16 and int(token, 16) >= 0


def test_review_binding_tolerance():
    binding = {"expected_total": 1000, "tolerance": 0.05}
    assert review_binding(binding, 1000) is True
    assert review_binding(binding, 1040) is True   # 容差内（4%）
    assert review_binding(binding, 1051) is False  # 超容差（5.1%）
    assert review_binding({"expected_total": 0}, 0) is True
    assert review_binding({"expected_total": 0}, 1) is False
    with pytest.raises(ValueError, match="expected_total"):
        review_binding({}, 100)


# ---------------------------------------------------------------- 端到端逐行复现（门禁 1 管线级）


def _pipeline_rows(root_seed: int) -> list[tuple]:
    """users → orders 管线：键区间 + 分布采样绑定 + 字段采样（seed 链路全打通）。"""
    from ptde.generator.samplers import sample_params

    users = list(range(1, 201))
    dist = DistributionFit(type="uniform", params={"min": 1, "max": 9})
    owner = assign_children(
        users, 500, field_rng(root_seed, "orders", "orders-0001", "user_id"), dist
    )
    amount_rng = field_rng(root_seed, "orders", "orders-0001", "amount")
    amounts = [
        round(float(sample_params("normal", {"mean": 100.0, "sigma": 20.0}, amount_rng)), 2)
        for _ in range(500)
    ]
    return list(zip(owner, amounts, strict=True))


def test_end_to_end_row_level_reproducibility():
    """同 root_seed 两次构建逐行一致；不同 seed 不同（门禁 1 管线级口径）。"""
    assert _pipeline_rows(42) == _pipeline_rows(42)
    assert _pipeline_rows(42) != _pipeline_rows(7)
