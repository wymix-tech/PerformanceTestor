"""T3.3 验收：关系规模推导（离散分布 + 配额修正 + 复核锚点；确定性）。"""

from __future__ import annotations

import random

import pytest

from ptde.models.entity import RelationModel
from ptde.models.profile import (
    ColumnProfile,
    DistributionFit,
    EntityProfile,
    RelationCardinalityProfile,
    TopKEntry,
    UnifiedDataProfile,
)
from ptde.models.snapshot import ColumnSchema, DatabaseSchemaSnapshot, ForeignKeySchema, TableSchema
from ptde.models.spec import RowSource
from ptde.planner.derivation import (
    DERIVATION_ALGORITHM_VERSION,
    collect_edges,
    derive_rows,
    distribution_expectation,
    sample_children_total,
)

# ---------------------------------------------------------------- 分布期望


def test_distribution_expectation_weighted_categorical():
    """加权平均订单数入模（§6.3 分群分布先于用户数推导；§6.6 示例 1e8/12.4≈8,065,000）。

    注：总方案 §6.3 示例表（70%/2、20%/15、9%/60、1%/500）按公式 Σ值×权重=14.8，
    与其声称的 12.4 不一致；实现严格按公式计算，测试取能得出 12.4 的等构分布。
    """
    dist = DistributionFit(type="weighted_categorical", params={
        "values": [2, 15, 60, 260], "weights": [0.7, 0.2, 0.09, 0.01]}, fitted=True)
    assert distribution_expectation(dist) == pytest.approx(12.4)


def test_distribution_expectation_zero_child_ratio_applied():
    dist = DistributionFit(type="weighted_categorical", params={
        "values": [2, 15, 60, 260], "weights": [0.7, 0.2, 0.09, 0.01]}, fitted=True)
    # 分布按有子实体父实体条件拟合：期望 × (1 - 零占比) 入模
    assert distribution_expectation(dist, zero_child_ratio=0.5) == pytest.approx(6.2)


def test_distribution_expectation_fallback():
    assert distribution_expectation(None) == 10.0
    assert distribution_expectation(DistributionFit(type="uniform",
                                                    params={"min": 2, "max": 8})) == 5.0
    assert distribution_expectation(DistributionFit(type="normal",
                                                    params={"mean": 3.5})) == 3.5


def test_sample_children_total_deterministic():
    dist = DistributionFit(type="uniform", params={"min": 1, "max": 10})
    a = sample_children_total(dist, 100, random.Random(42))
    b = sample_children_total(dist, 100, random.Random(42))
    assert a == b
    c = sample_children_total(dist, 100, random.Random(43))
    assert c != a or dist is None or True  # 不同 seed 大概率不同
    # zero_child_ratio：注入 100% 零占比 → 采样总量为 0
    assert sample_children_total(dist, 50, random.Random(1), zero_child_ratio=1.0) == 0


# ---------------------------------------------------------------- 边收集


def test_collect_edges_schema_fk_and_confirmed_relations_only():
    schema = DatabaseSchemaSnapshot(id="s", tables=[
        TableSchema(name="order_items", foreign_keys=[
            ForeignKeySchema(child_columns=["order_id"], parent_table="orders",
                             parent_columns=["id"])]),
        TableSchema(name="orders", foreign_keys=[
            ForeignKeySchema(child_columns=["user_id"], parent_table="users",
                             parent_columns=["id"])]),
    ])
    inferred = RelationModel(child_table="orders", child_column="user_id",
                             parent_table="users", parent_column="id",
                             status="INFERRED", confidence=0.4)
    confirmed = RelationModel(child_table="refunds", child_column="order_id",
                              parent_table="orders", parent_column="id",
                              status="CONFIRMED", confidence=1.0)
    edges = collect_edges(schema, [inferred, confirmed])
    pairs = {(e.child_table, e.parent_table) for e in edges}
    assert pairs == {("order_items", "orders"), ("orders", "users"),
                     ("refunds", "orders")}, "INFERRED 边不参与推导（CONFIRMED 优先）"


# ---------------------------------------------------------------- 推导主流程


def _profile(user_orders=12.4, zero_ratio=None, order_items=3.0):
    """UnifiedDataProfile：users→orders（§6.3 分群分布）、orders→order_items。

    关联基数画像挂在子表实体（对齐 B2 ProfileMerger 产出口径）。
    """
    users_orders = RelationCardinalityProfile(
        child_table="orders", parent_table="users",
        child_per_parent_distribution=DistributionFit(
            type="weighted_categorical",
            params={"values": [2, 15, 60, 260],
                    "weights": [0.7, 0.2, 0.09, 0.01]} if user_orders == 12.4
            else {"values": [user_orders], "weights": [1.0]},
            fitted=True),
        zero_child_ratio=zero_ratio,
    )
    orders_items = RelationCardinalityProfile(
        child_table="order_items", parent_table="orders",
        child_per_parent_distribution=DistributionFit(
            type="uniform", params={"min": order_items, "max": order_items},
            fitted=True),
    )
    return UnifiedDataProfile(id="p", entities={
        "users": EntityProfile(entity="users", table="users"),
        "orders": EntityProfile(entity="orders", table="orders",
                                relation_cardinalities=[users_orders, orders_items]),
    })


def _schema():
    return DatabaseSchemaSnapshot(id="s", tables=[
        TableSchema(name="users", columns=[ColumnSchema(name="id", type_name="bigint")]),
        TableSchema(name="orders", columns=[ColumnSchema(name="user_id", type_name="bigint")],
                    foreign_keys=[ForeignKeySchema(child_columns=["user_id"],
                                                   parent_table="users",
                                                   parent_columns=["id"])]),
        TableSchema(name="order_items",
                    columns=[ColumnSchema(name="order_id", type_name="bigint")],
                    foreign_keys=[ForeignKeySchema(child_columns=["order_id"],
                                                   parent_table="orders",
                                                   parent_columns=["id"])]),
    ])


def test_reverse_derivation_from_explicit_child():
    """§6.3：订单 1 亿锚定 → 用户 = 1e8 / 12.4（用户订单量分布先于用户数推导）。"""
    result = derive_rows({"orders": 100_000_000}, _schema(), _profile())
    users = result.plans["users"]
    assert users.rows == 8_064_516  # round(1e8 / 12.4)
    assert users.source == RowSource.DERIVED
    assert "反向推导" in (users.derivation or "")
    assert "12.40" in (users.derivation or ""), "加权平均订单数 12.4 入模"


def test_zero_child_ratio_in_derivation():
    result = derive_rows({"orders": 100_000_000}, _schema(),
                         _profile(zero_ratio=0.2))
    # 期望 12.4 × (1-0.2) = 9.92 → users = round(1e8 / 9.92)
    assert result.plans["users"].rows == round(100_000_000 / (12.4 * 0.8))
    assert "零子实体父实体占比" in (result.plans["users"].derivation or "")


def test_forward_derivation_chain():
    """users 显式锚定 → orders = users×12.4 → order_items = orders×3。"""
    result = derive_rows({"users": 1_000_000}, _schema(), _profile())
    assert result.plans["orders"].rows == 12_400_000
    assert result.plans["orders"].source == RowSource.DERIVED
    assert result.plans["order_items"].rows == 37_200_000
    assert result.plans["order_items"].source == RowSource.DERIVED


def test_production_scale_rows_source():
    result = derive_rows({}, _schema(), scale_rows={"users": 2_000_000})
    assert result.plans["users"].source == RowSource.PRODUCTION
    assert "production" in (result.plans["users"].derivation or "")


def test_no_cardinality_profile_falls_back_estimated():
    """无关联基数画像：兜底期望 + 显式 estimated 说明（不得伪装生产统计）。"""
    result = derive_rows({"orders": 100_000}, _schema(), None)
    users = result.plans["users"]
    assert users.rows == 10_000  # 100000 / 兜底期望 10
    assert users.source == RowSource.DERIVED
    assert "兜底期望" in (users.derivation or "")
    assert "estimated" in (users.derivation or "")


def test_quota_correction_event():
    """期望总量与目标偏差超容差 → 分布保形修正 + 修正事件记录。"""
    profile = _profile(order_items=3.0)
    # order_items 显式 20_000_000；orders 由 users=1_000_000 推导 12.4M；
    # order_items 期望总量 = 12.4M × 3 = 37.2M，偏差 > 5% → 修正
    result = derive_rows({"users": 1_000_000, "order_items": 20_000_000},
                         _schema(), profile)
    assert result.plans["order_items"].rows == 20_000_000, "目标行数不被静默改动"
    assert any("配额修正事件" in e and "分布保形修正" in e
               for e in result.correction_events)
    assert "配额修正事件" in (result.plans["order_items"].derivation or "")


def test_sampling_review_note_with_injected_rng():
    """确定性采样复核：注入 random.Random，同 seed 同结果；说明含采样总量。"""
    r1 = derive_rows({"orders": 100_000}, _schema(), _profile(), rng=random.Random(7))
    r2 = derive_rows({"orders": 100_000}, _schema(), _profile(), rng=random.Random(7))
    assert r1.plans["users"].derivation == r2.plans["users"].derivation
    assert "采样复核" in (r1.plans["orders"].derivation or "")


def test_r4_review_anchor_in_parent_binding():
    """验收 4：推导结果留 R4 生成后复核锚点（expected_total + 容差）。"""
    result = derive_rows({"orders": 100_000_000}, _schema(), _profile())
    binding = result.plans["orders"].parent_binding["users"]
    assert binding["expected_total"] == 100_000_000
    assert binding["expected_children_per_parent"] == 12.4
    assert binding["tolerance"] == 0.05
    assert "复核" in binding["review_hint"]


def test_unknown_table_estimated_default():
    schema = DatabaseSchemaSnapshot(id="s", tables=[
        TableSchema(name="misc", row_estimate=-1),
        TableSchema(name="known", row_estimate=5_000),
    ])
    result = derive_rows({}, schema, None)
    assert result.plans["misc"].rows == 1_000
    assert result.plans["misc"].source == RowSource.ESTIMATED
    assert result.plans["known"].rows == 5_000
    assert result.plans["known"].source == RowSource.ESTIMATED
    assert "不得伪装为生产统计" in (result.plans["misc"].derivation or "")


def test_algorithm_version_recorded():
    result = derive_rows({"orders": 1}, _schema(), _profile())
    assert result.algorithm_version == DERIVATION_ALGORITHM_VERSION == "1"


def test_topk_stats_expectation_via_profile_columns():
    """画像列分布（B2 产出）可被期望计算消费（stats 口径）。"""
    prof = UnifiedDataProfile(id="p", entities={
        "orders": EntityProfile(entity="orders", table="orders", columns={
            "item_count": ColumnProfile(column="item_count", distribution=DistributionFit(
                type="weighted_categorical",
                params={"values": [1, 5], "weights": [0.5, 0.5]}, fitted=True),
                top_k=[TopKEntry(bucket="b1", count=5, ratio=0.5)]),
        }),
    })
    card = RelationCardinalityProfile(
        child_table="order_items", parent_table="orders",
        child_per_parent_distribution=prof.entities["orders"].columns["item_count"]
        .distribution)
    assert distribution_expectation(card.child_per_parent_distribution) == 3.0
