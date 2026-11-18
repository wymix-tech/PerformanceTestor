"""T3.6 验收：分布规划（每字段类型+参数+来源；条件分布对识别为 R4 依赖图输入）。"""

from __future__ import annotations

from ptde.models.profile import (
    ColumnProfile,
    DistributionFit,
    EntityProfile,
    TopKEntry,
    UnifiedDataProfile,
)
from ptde.models.snapshot import ColumnSchema, DatabaseSchemaSnapshot, TableSchema
from ptde.planner.distribution_planner import (
    identify_conditional_pairs,
    plan_distributions,
)

# ---------------------------------------------------------------- 分布计划


def _schema():
    return DatabaseSchemaSnapshot(id="s", tables=[
        TableSchema(name="orders", columns=[
            ColumnSchema(name="id", type_name="bigint"),
            ColumnSchema(name="status", type_name="varchar(32)"),
            ColumnSchema(name="amount", type_name="decimal(10,2)"),
            ColumnSchema(name="paid_at", type_name="datetime", nullable=True),
        ])])


def _profile(with_status_dist=True):
    status_dist = DistributionFit(type="weighted_categorical", params={
        "values": ["PAID", "PENDING"], "weights": [0.8, 0.2]}, fitted=True)
    orders = EntityProfile(entity="orders", table="orders", columns={
        "id": ColumnProfile(column="id", distinct_count=1000),
        "status": ColumnProfile(column="status", distribution=status_dist,
                                top_k=[TopKEntry(bucket="PAID", count=8, ratio=0.8)]),
        "paid_at": ColumnProfile(column="paid_at", distribution=DistributionFit(
            type="temporal", params={"peak_hours": [10, 20]}, fitted=True),
            null_ratio=0.2),
    })
    if not with_status_dist:
        # 仅有 Top-K（无拟合分布）：验证 Top-K → WeightedCategorical 合成路径
        orders.columns["status"] = ColumnProfile(
            column="status",
            top_k=[TopKEntry(bucket="PAID", count=8, ratio=0.8)])
    return UnifiedDataProfile(id="p", entities={"orders": orders})


def _entries(entries):
    return {(e.entity, e.field): e for e in entries}


def test_plan_distributions_stats_source():
    entries = _entries(plan_distributions(_profile(), _schema()))
    st = entries[("orders", "status")]
    assert st.source == "stats"
    assert st.distribution.type == "weighted_categorical"
    tm = entries[("orders", "paid_at")]
    assert tm.source == "stats" and tm.distribution.type == "temporal"


def test_plan_distributions_topk_fallback_categorical():
    """仅有 Top-K（无拟合分布）→ 合成 WeightedCategorical，source=stats。"""
    profile = _profile(with_status_dist=False)
    entries = _entries(plan_distributions(profile, _schema()))
    st = entries[("orders", "status")]
    assert st.source == "stats"
    assert st.distribution.type == "weighted_categorical"
    assert st.distribution.params["values"] == ["PAID"]


def test_plan_distributions_estimated_defaults():
    """无统计依据 → 显式默认分布（fitted=False，source=estimated，不伪装统计）。"""
    entries = _entries(plan_distributions(None, _schema()))
    assert entries[("orders", "id")].source == "estimated"
    assert entries[("orders", "id")].distribution.type == "uniform"
    assert entries[("orders", "id")].distribution.fitted is False
    assert entries[("orders", "amount")].distribution.params["precision"] == 2
    assert entries[("orders", "status")].distribution.params["max_length"] == 64


def test_plan_distributions_overrides():
    """用户目标覆盖统计（§4.4）：source=estimated，分布为覆盖值。"""
    override = DistributionFit(type="uniform", params={"min": 0, "max": 100},
                               fitted=False)
    entries = _entries(plan_distributions(_profile(), _schema(),
                                          {"orders.amount": override}))
    assert entries[("orders", "amount")].distribution is override
    assert entries[("orders", "amount")].source == "estimated"
    assert entries[("orders", "status")].source == "stats", "未覆盖字段仍取统计"


# ---------------------------------------------------------------- 条件分布对


def test_identify_pairs_profile_declared():
    profile = UnifiedDataProfile(id="p", entities={"orders": EntityProfile(
        entity="orders", table="orders", columns={
            "paid_at": ColumnProfile(column="paid_at", distribution=DistributionFit(
                type="conditional", params={"depends_on": ["status"]}, fitted=True))})})
    pairs = identify_conditional_pairs(profile, _schema())
    assert [(p.entity, p.dependent, p.depends_on, p.basis) for p in pairs] \
        == [("orders", "paid_at", ["status"], "profile_declared")]


def test_identify_pairs_configured():
    pairs = identify_conditional_pairs(None, _schema(),
                                       [("orders", "paid_at", ["status"])])
    assert pairs[0].basis == "configured"


def test_identify_pairs_heuristic_status_temporal():
    """启发式候选：status 类别列（小基数）+ 带 NULL 的时间列。"""
    profile = _profile()
    profile.entities["orders"].columns["status"].distinct_count = 2
    profile.entities["orders"].columns["paid_at"].distribution = DistributionFit(
        type="temporal", params={}, fitted=False)
    profile.entities["orders"].columns["paid_at"].null_ratio = 0.2
    pairs = identify_conditional_pairs(profile, _schema())
    assert any(p.dependent == "paid_at" and p.depends_on == ["status"]
               and p.basis == "heuristic" for p in pairs)


def test_identify_pairs_no_false_positive():
    """时间列无 NULL（不可能条件未付）→ 不产生启发式候选。"""
    profile = _profile()
    profile.entities["orders"].columns["status"].distinct_count = 2
    profile.entities["orders"].columns["paid_at"].null_ratio = 0.0
    pairs = identify_conditional_pairs(profile, _schema())
    assert not any(p.basis == "heuristic" for p in pairs)


def test_pairs_deduped_by_entity_field():
    profile = _profile()
    profile.entities["orders"].columns["status"].distinct_count = 2
    profile.entities["orders"].columns["paid_at"].distribution = DistributionFit(
        type="conditional", params={"depends_on": ["status"]}, fitted=True)
    pairs = identify_conditional_pairs(
        profile, _schema(), [("orders", "paid_at", ["status"])])
    paid = [p for p in pairs if p.dependent == "paid_at"]
    assert len(paid) == 1, "画像声明优先，配置/启发式去重"
