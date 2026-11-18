"""T3.7 验收：GenerationPlan 产出与冲突诊断（实施方案 §6.5 验收门禁逐条对照）。"""

from __future__ import annotations

import random

import pytest

from ptde.models.profile import (
    DistributionFit,
    EntityProfile,
    RelationCardinalityProfile,
    UnifiedDataProfile,
)
from ptde.models.snapshot import ColumnSchema, DatabaseSchemaSnapshot, ForeignKeySchema, TableSchema
from ptde.models.spec import DatasetSpec, GenerationPlan, RowSource
from ptde.planner.planner import DatasetPlanner, build_plan
from ptde.planner.solver import PlanningConflict
from ptde.planner.storage_estimator import CalibratedRowCost
from ptde.planner.workload import StressParams, TableDemand

GB = 1024**3


def _schema(row_estimate=-1):
    return DatabaseSchemaSnapshot(id="s", tables=[
        TableSchema(name="users", row_estimate=row_estimate,
                    columns=[ColumnSchema(name="id", type_name="bigint")]),
        TableSchema(name="orders",
                    columns=[ColumnSchema(name="user_id", type_name="bigint")],
                    foreign_keys=[ForeignKeySchema(child_columns=["user_id"],
                                                   parent_table="users",
                                                   parent_columns=["id"])]),
    ])


def _profile(per_user=10.0):
    return UnifiedDataProfile(id="p", entities={
        "orders": EntityProfile(entity="orders", table="orders",
                                relation_cardinalities=[
                                    RelationCardinalityProfile(
                                        child_table="orders", parent_table="users",
                                        child_per_parent_distribution=DistributionFit(
                                            type="uniform",
                                            params={"min": per_user, "max": per_user},
                                            fitted=True))]),
    })


def _spec(rows: dict, **kw) -> DatasetSpec:
    return DatasetSpec(name="ecom", targets={"rows": rows}, **kw)


# ---------------------------------------------------------------- 验收 3：无 100 万钳制


def test_five_million_rows_planned_successfully():
    """A1 分轨：新管线 500 万行 DatasetSpec 规划成功（无 100 万钳制）。"""
    spec = _spec({"orders": 5_000_000})
    plan = DatasetPlanner(spec, schema=_schema()).plan()
    assert plan.entities["orders"].rows == 5_000_000
    assert plan.entities["orders"].source == RowSource.EXPLICIT
    assert plan.conflict_errors == []
    # 对照：legacy workload 收编副本保留钳制语义（A1 分轨，legacy 不动）
    from ptde.planner import workload as wl
    est = wl.estimate(StressParams(qps_limit=1_000_000.0, duration_sec=100),
                      [TableDemand("orders", True, page_size=10)])
    assert est.rows_by_table["orders"] <= 200_000, "legacy 收编副本钳制语义保留"


# ---------------------------------------------------------------- 验收 2：冲突显式失败


def test_unsatisfiable_conflict_lists_each_violation():
    """构造「1 亿订单 vs 200GB 存储」不可满足用例：PlanningConflict 逐条冲突。"""
    spec = DatasetSpec(
        name="huge",
        targets={
            "rows": {"orders": 100_000_000},
            "storage": {"max_total": "200GB", "include_indexes": True},
        },
        constraints={"max_staging_storage_bytes": 150 * GB},
    )
    calibration = {"orders": CalibratedRowCost(
        table="orders", bytes_per_row=2000.0, index_bytes_per_row=800.0,
        sample_rows=10_000, error_band="±2.0%（两轮扩样校准）")}
    with pytest.raises(PlanningConflict) as exc:
        DatasetPlanner(spec, schema=_schema(), calibration=calibration).plan()
    assert len(exc.value.conflicts) == 2, "staging 约束与存储目标两条冲突逐条列出"
    msg = str(exc.value)
    assert "max_staging_storage" in msg and "200GB" in msg
    assert "2 条冲突" in msg


def test_conflict_does_not_mutate_targets():
    """冲突路径不静默收缩：spec.targets.rows 保持原值。"""
    spec = _spec({"orders": 100_000_000},
                 constraints={"max_staging_storage_bytes": 1})
    with pytest.raises(PlanningConflict):
        DatasetPlanner(spec, schema=_schema(),
                       calibration={"orders": CalibratedRowCost(
                           table="orders", bytes_per_row=1000.0, sample_rows=1)}).plan()
    assert spec.targets.rows == {"orders": 100_000_000}


# ---------------------------------------------------------------- 验收 1：来源标注与推导说明


def test_every_table_has_source_and_derivation():
    """每张表规模带 source 标注与推导说明；导出 GenerationPlan 逐表可查。"""
    spec = _spec({"orders": 1_000_000})
    plan = DatasetPlanner(spec, schema=_schema(), profile=_profile(per_user=10.0)).plan()
    assert plan.entities["orders"].source == RowSource.EXPLICIT
    assert "用户显式指定" in (plan.entities["orders"].derivation or "")
    assert plan.entities["users"].source == RowSource.DERIVED
    assert plan.entities["users"].rows == 100_000
    assert "反向推导" in (plan.entities["users"].derivation or "")
    exported = plan.to_yaml()
    assert "source: explicit" in exported and "source: derived" in exported
    restored = GenerationPlan.from_json(plan.to_json())
    for table, ep in restored.entities.items():
        assert ep.source in ("explicit", "production", "derived", "estimated")
        assert ep.derivation, f"表 {table} 缺推导说明"


def test_production_source_from_scale_factor():
    spec = DatasetSpec(name="ecom", targets={
        "rows": {"orders": 1_000_000},
        "scale_factor": {"factors": {"users": 2.0}},
    })
    schema = _schema(row_estimate=80_000)
    plan = DatasetPlanner(spec, schema=schema).plan()
    assert plan.entities["users"].rows == 160_000
    assert plan.entities["users"].source == RowSource.PRODUCTION


# ---------------------------------------------------------------- 验收 4：R4 复核锚点


def test_review_anchor_survives_export():
    """推导结果留 R4 生成后复核锚点（数量容差内），导出后可查。"""
    plan = DatasetPlanner(_spec({"orders": 1_000_000}), schema=_schema(),
                          profile=_profile(per_user=10.0)).plan()
    binding = plan.entities["orders"].parent_binding["users"]
    assert binding["expected_total"] == 1_000_000
    assert binding["tolerance"] == 0.05
    restored = GenerationPlan.from_yaml(plan.to_yaml())
    assert restored.entities["orders"].parent_binding["users"]["expected_total"] \
        == 1_000_000


# ---------------------------------------------------------------- 验收 5：容量估算校准


def test_storage_calibrated_and_band():
    calibration = {
        "orders": CalibratedRowCost(table="orders", bytes_per_row=300.0,
                                    index_bytes_per_row=100.0, sample_rows=10_000,
                                    error_band="±3.0%（两轮扩样校准）"),
        "users": CalibratedRowCost(table="users", bytes_per_row=200.0,
                                   index_bytes_per_row=40.0, sample_rows=10_000,
                                   error_band="±3.0%（两轮扩样校准）"),
    }
    plan = DatasetPlanner(_spec({"orders": 1_000_000}), schema=_schema(),
                          calibration=calibration).plan()
    assert plan.storage.calibrated is True
    # orders 1e6×(300+100) + users 1e5×(200+40) = 4.24e8（实测 B_i，非字段长度相加）
    assert plan.storage.estimated_total_size_bytes == 400_000_000 + 24_000_000
    assert plan.storage.confidence == 0.9


def test_storage_uncalibrated_warning():
    plan = DatasetPlanner(_spec({"orders": 1_000}), schema=_schema()).plan()
    assert plan.storage.calibrated is False
    assert any("未经试装载实测" in w for w in plan.warnings)


def test_pilot_report_roundtrip_seeds_calibration():
    """Pilot Run 报告可序列化落库并回填校准（B_i 非字段长度相加值）。"""
    from ptde.planner.pilot_run import PilotRunConfig, run_pilot
    from tests.test_ptde_planner_storage import FakeConn, FakeProbe

    conn = FakeConn()
    report = run_pilot("orders", [("id", "bigint"), ("status", "varchar(32)")],
                       lambda: conn,
                       config=PilotRunConfig(sample_rows=500, scale_multiplier=10,
                                             batch_size=100),
                       probe=FakeProbe(), rng=random.Random(0))
    cost = report.to_calibration()
    users_cost = CalibratedRowCost(table="users", bytes_per_row=200.0,
                                   index_bytes_per_row=40.0, sample_rows=10_000,
                                   error_band=report.error_band)
    plan = DatasetPlanner(_spec({"orders": 10_000}), schema=_schema(),
                          calibration={"orders": cost, "users": users_cost}).plan()
    assert plan.storage.calibrated is True
    assert report.rows_verified is True


# ---------------------------------------------------------------- 工作负载分支 / 分布 / 告警


def test_workload_branch_with_facts():
    spec = DatasetSpec(name="w", targets={"workload": {"scenarios": [
        {"id": "s1", "weight": 1.0}]}})
    plan = DatasetPlanner(spec, schema=_schema()).plan(
        workload_params=StressParams(qps_limit=100.0, duration_sec=60),
        workload_demands=[TableDemand("orders", True, page_size=20)])
    assert plan.entities["orders"].rows == 6000
    assert plan.entities["orders"].source == RowSource.ESTIMATED
    assert "工作负载推导" in (plan.entities["orders"].derivation or "")


def test_workload_branch_without_facts_warns_llm_suggestion_only():
    spec = DatasetSpec(name="w", targets={"workload": {"scenarios": [
        {"id": "s1", "weight": 1.0}]}})
    plan = DatasetPlanner(spec, schema=_schema()).plan()
    assert any("LLM 增强为建议定位，不进确定性规划路径" in w for w in plan.warnings)
    assert "llm" not in plan.dataset_spec_fingerprint


def test_conditional_pair_in_distribution_plan():
    spec = _spec({"orders": 1000})
    plan = DatasetPlanner(spec, schema=_schema(), profile=_profile()).plan(
        conditional_pairs=[("orders", "paid_at", ["status"])])
    cond = [e for e in plan.distribution_plan
            if e.field == "paid_at" and e.distribution.type == "conditional"]
    assert len(cond) == 1
    assert cond[0].distribution.params["depends_on"] == ["status"]
    assert cond[0].source == "estimated"


def test_warnings_and_conflict_errors_separated():
    """成功计划：warnings 承载告警、conflict_errors 为空（分离口径）。"""
    plan = DatasetPlanner(_spec({"orders": 1_000}), schema=_schema()).plan()
    assert plan.conflict_errors == []
    assert any("规划算法版本" in w for w in plan.warnings)
    assert any(w.startswith("planner=") or "planner=" in w for w in plan.warnings)


def test_deterministic_planning_same_seed():
    """同 seed 两次规划结果一致（确定性推导 + 注入随机源）。"""
    spec = _spec({"orders": 100_000}, reproducibility={"seed": 42})
    p1 = DatasetPlanner(spec, schema=_schema(), profile=_profile(),
                        rng=random.Random(42)).plan()
    p2 = DatasetPlanner(spec, schema=_schema(), profile=_profile(),
                        rng=random.Random(42)).plan()
    assert p1.entities["users"].rows == p2.entities["users"].rows
    assert p1.entities["users"].derivation == p2.entities["users"].derivation


def test_build_plan_convenience_and_export():
    spec = _spec({"orders": 1_000_000})
    plan = build_plan(spec, schema=_schema(), profile=_profile(), plan_id="plan-001")
    assert plan.id == "plan-001"
    assert plan.fingerprint and len(plan.fingerprint) == 64
    restored = GenerationPlan.from_yaml(plan.to_yaml())
    assert restored.id == "plan-001"
    assert restored.compute_fingerprint() == plan.fingerprint
