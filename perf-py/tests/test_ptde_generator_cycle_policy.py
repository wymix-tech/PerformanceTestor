"""T4.7 验收：循环依赖显式安全策略（废除 break_weakest_cycle_edge 静默剔边）。

- 自引用环 → 带内自引用（SELF_REF_MAX_DEPTH=5 深度上限，legacy 语义继承）；
- 环内全可空 → 可空回填（两阶段）；
- 不可空 + 延迟约束能力 → 延迟约束；
- 不可空 + 显式批准关闭约束 → 分阶段装载（总方案风险表：需显式批准）；
- 无安全方式 → CyclePolicyError 逐边输出原因，阻止执行不剔边。
"""

from __future__ import annotations

import pytest

from ptde.generator.cycle_policy import (
    SELF_REF_MAX_DEPTH,
    CycleFkEdge,
    CyclePolicyError,
    CycleResolution,
    CycleStrategy,
    project_fk_edges,
    resolve_cycle,
    resolve_cycles,
)
from ptde.models.snapshot import (
    ColumnSchema,
    DatabaseSchemaSnapshot,
    ForeignKeySchema,
    TableSchema,
)

# ---------------------------------------------------------------- 自引用


def test_self_reference_in_band_with_depth_cap():
    r = resolve_cycle(["category"], [CycleFkEdge("category", "parent_id", nullable=False)])
    assert r.strategy is CycleStrategy.SELF_REFERENCE
    assert r.depth == SELF_REF_MAX_DEPTH == 5
    assert "SELF_REF_MAX_DEPTH=5" in r.steps[0]


# ---------------------------------------------------------------- 可空回填


def test_nullable_backfill_for_all_nullable_cycle():
    edges = [
        CycleFkEdge("a", "fk_b", nullable=True),
        CycleFkEdge("b", "fk_c", nullable=True),
        CycleFkEdge("c", "fk_a", nullable=True),
    ]
    r = resolve_cycle(["a", "b", "c"], edges)
    assert isinstance(r, CycleResolution)
    assert r.strategy is CycleStrategy.NULLABLE_BACKFILL
    assert r.cycle == ("a", "b", "c")
    assert len(r.steps) == 2
    assert "NULL" in r.steps[0] and "回填" in r.steps[1]
    assert r.requires_backfill


# ---------------------------------------------------------------- 延迟约束


def test_deferred_constraint_when_supported():
    edges = [
        CycleFkEdge("a", "fk_b", nullable=False),
        CycleFkEdge("b", "fk_a", nullable=False),
    ]
    r = resolve_cycle(["a", "b"], edges, supports_deferred_constraints=True)
    assert r.strategy is CycleStrategy.DEFERRED_CONSTRAINT
    assert r.requires_backfill


# ---------------------------------------------------------------- 分阶段装载（需显式批准）


def test_staged_load_requires_explicit_approval():
    edges = [
        CycleFkEdge("a", "fk_b", nullable=False),
        CycleFkEdge("b", "fk_a", nullable=False),
    ]
    r = resolve_cycle(["a", "b"], edges, allow_constraint_disabled=True)
    assert r.strategy is CycleStrategy.STAGED_LOAD
    assert "显式批准" in r.steps[0]


# ---------------------------------------------------------------- 无安全方式：阻止执行


def test_block_when_no_safe_strategy():
    edges = [
        CycleFkEdge("a", "fk_b", nullable=False),
        CycleFkEdge("b", "fk_a", nullable=False),
    ]
    with pytest.raises(CyclePolicyError) as exc:
        resolve_cycle(["a", "b"], edges)
    err = exc.value
    assert err.cycle == ["a", "b"]
    assert any("a.fk_b NOT NULL" in r for r in err.reasons)
    assert any("延迟约束" in r for r in err.reasons)
    assert any("显式批准" in r for r in err.reasons)
    assert "禁止静默剔边" in str(err)


def test_block_reports_missing_nullability_info():
    edges = [CycleFkEdge("a", "fk_b", nullable=True)]  # b 缺边信息
    with pytest.raises(CyclePolicyError) as exc:
        resolve_cycle(["a", "b"], edges)
    assert any("缺少环内 FK 列可空性信息" in r for r in exc.value.reasons)


# ---------------------------------------------------------------- 批量判定


def test_resolve_cycles_batch():
    cycles_of = [["category"], ["a", "b"]]
    edges = [
        CycleFkEdge("category", "parent_id", nullable=False),
        CycleFkEdge("a", "fk_b", nullable=True),
        CycleFkEdge("b", "fk_a", nullable=True),
    ]
    resolutions = resolve_cycles(cycles_of, edges)
    assert resolutions[("category",)].strategy is CycleStrategy.SELF_REFERENCE
    assert resolutions[("a", "b")].strategy is CycleStrategy.NULLABLE_BACKFILL
    # 任一环无安全方式即抛错（阻止执行）
    bad = [
        CycleFkEdge("a", "fk_b", nullable=False),
        CycleFkEdge("b", "fk_a", nullable=False),
    ]
    with pytest.raises(CyclePolicyError):
        resolve_cycles([["a", "b"]], bad)


# ---------------------------------------------------------------- Schema 快照投影（遗留问题 2）


def _snapshot(tables: list[TableSchema]) -> DatabaseSchemaSnapshot:
    return DatabaseSchemaSnapshot(id="snap-1", tables=tables)


def test_project_fk_edges_from_schema_snapshot():
    snap = _snapshot([
        TableSchema(
            name="orders",
            columns=[
                ColumnSchema(name="id", type_name="BIGINT", nullable=False),
                ColumnSchema(name="customer_id", type_name="BIGINT", nullable=False),
            ],
            foreign_keys=[ForeignKeySchema(parent_table="customers", child_columns=["customer_id"])],
        ),
        TableSchema(
            name="customers",
            columns=[ColumnSchema(name="id", type_name="BIGINT", nullable=False)],
        ),
    ])
    edges = project_fk_edges(snap, ["orders", "customers"])
    assert edges == [CycleFkEdge("orders", "customer_id", nullable=False)]


def test_project_fk_edges_composite_fk_expands_per_column():
    snap = _snapshot([
        TableSchema(
            name="t1",
            columns=[
                ColumnSchema(name="a_id", type_name="BIGINT", nullable=True),
                ColumnSchema(name="b_code", type_name="VARCHAR", nullable=False),
            ],
            foreign_keys=[ForeignKeySchema(parent_table="t0", child_columns=["a_id", "b_code"])],
        ),
        TableSchema(name="t0", columns=[]),
    ])
    edges = project_fk_edges(snap, ["t1"])
    assert edges == [
        CycleFkEdge("t1", "a_id", nullable=True),
        CycleFkEdge("t1", "b_code", nullable=False),
    ]


def test_project_fk_edges_member_missing_from_snapshot_yields_no_edges():
    snap = _snapshot([TableSchema(name="a", columns=[])])
    # b 不在快照中 → 无边产出；resolve_cycle 按「信息不足」显式失败
    edges = project_fk_edges(snap, ["a", "b"])
    assert edges == []
    with pytest.raises(CyclePolicyError) as exc:
        resolve_cycle(["a", "b"], edges)
    assert any("缺少环内 FK 列可空性信息" in r for r in exc.value.reasons)


def test_project_fk_edges_missing_child_column_explicit_failure():
    snap = _snapshot([
        TableSchema(
            name="a",
            columns=[ColumnSchema(name="id", type_name="BIGINT", nullable=False)],
            foreign_keys=[ForeignKeySchema(parent_table="b", child_columns=["ghost_col"])],
        ),
        TableSchema(name="b", columns=[]),
    ])
    with pytest.raises(ValueError, match="ghost_col"):
        project_fk_edges(snap, ["a"])


def test_project_fk_edges_feeds_resolve_cycle_end_to_end():
    # 双向可空环：快照投影 → resolve_cycle 判定可空回填（投影接入主链路）
    snap = _snapshot([
        TableSchema(
            name="a",
            columns=[ColumnSchema(name="fk_b", type_name="BIGINT", nullable=True)],
            foreign_keys=[ForeignKeySchema(parent_table="b", child_columns=["fk_b"])],
        ),
        TableSchema(
            name="b",
            columns=[ColumnSchema(name="fk_a", type_name="BIGINT", nullable=True)],
            foreign_keys=[ForeignKeySchema(parent_table="a", child_columns=["fk_a"])],
        ),
    ])
    r = resolve_cycle(["a", "b"], project_fk_edges(snap, ["a", "b"]))
    assert r.strategy is CycleStrategy.NULLABLE_BACKFILL
