"""T3.1 验收：目标类型模型与校验（5 类目标可组合；互斥/冲突组合模型层拦截）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ptde.models.spec import DatasetSpec
from ptde.planner.targets import (
    check_target_combination,
    parse_storage_bytes,
    validate_planning_targets,
)


def _spec(targets: dict, **kw) -> DatasetSpec:
    return DatasetSpec(name="ecom", targets=targets, **kw)


# ---------------------------------------------------------------- 5 类目标可组合


def test_targets_combinable():
    """rows + storage + workload 可组合（总方案 §6.1：多种目标可以组合）。"""
    s = _spec({
        "rows": {"orders": 100_000_000},
        "storage": {"max_total": "200GB", "include_indexes": True},
        "workload": {"scenarios": [
            {"id": "a", "weight": 0.45}, {"id": "b", "weight": 0.35},
            {"id": "c", "weight": 0.20}]},
    })
    assert s.targets.rows["orders"] == 100_000_000
    assert s.targets.storage.max_total == "200GB"


def test_scale_factor_target_combinable():
    s = _spec({"rows": {"orders": 1_000_000}, "scale_factor": {"factors": {"users": 2.5}}})
    assert s.targets.scale_factor.factors == {"users": 2.5}


# ---------------------------------------------------------------- 互斥组合（模型层）


@pytest.mark.parametrize("conflicting", [
    {"rows": {"orders": 1_000_000}},
    {"scale_factor": {"factors": {"users": 2.0}}},
    {"workload": {"scenarios": [{"id": "a", "weight": 1.0}]}},
])
def test_auto_target_mutually_exclusive(conflicting):
    """AutoTarget 与显式目标互斥（自动规划与显式目标组合语义冲突）。"""
    targets = dict(conflicting)
    targets["auto"] = {"tiers": [{"name": "small", "target_rows": {"orders": 1000}}]}
    with pytest.raises(ValidationError, match="互斥"):
        _spec(targets)


def test_auto_target_alone_ok():
    s = _spec({"auto": {"tiers": [{"name": "small"}]}})
    assert s.targets.auto is not None


# ---------------------------------------------------------------- 数值与形态校验


def test_rows_and_factors_must_be_positive():
    with pytest.raises(ValidationError, match="目标行数必须为正"):
        _spec({"rows": {"orders": 0}})
    with pytest.raises(ValidationError, match="倍率必须为正"):
        _spec({"scale_factor": {"factors": {"users": -1.0}}})


def test_workload_weights_must_sum_to_one():
    with pytest.raises(ValidationError, match="权重合计必须为 1"):
        _spec({"workload": {"scenarios": [
            {"id": "a", "weight": 0.5}, {"id": "b", "weight": 0.6}]}})


def test_workload_weight_bounds():
    with pytest.raises(ValidationError, match=r"权重必须在 \(0, 1\] 区间"):
        _spec({"workload": {"scenarios": [{"id": "a", "weight": 1.5}]}})


def test_storage_max_total_format():
    with pytest.raises(ValidationError, match="形态非法"):
        _spec({"storage": {"max_total": "200 gigabytes"}})
    assert _spec({"storage": {"max_total": "200GB"}}).targets.storage is not None


# ---------------------------------------------------------------- 容量换算


def test_parse_storage_bytes():
    assert parse_storage_bytes("200GB") == 200 * 1024**3
    assert parse_storage_bytes("10mb") == 10 * 1024**2
    assert parse_storage_bytes("512 B") == 512
    assert parse_storage_bytes("2TB") == 2 * 1024**4
    with pytest.raises(ValueError, match="形态非法"):
        parse_storage_bytes("200 gigabytes")
    with pytest.raises(ValueError, match="形态非法"):
        parse_storage_bytes("")


# ---------------------------------------------------------------- 规划器侧组合检查


def test_planner_level_conflicts():
    """存储目标缺锚点 / 倍率缺生产基线 / 无任何目标 → 逐条冲突说明。"""
    s1 = _spec({"storage": {"max_total": "200GB"}})
    conflicts = validate_planning_targets(s1)
    assert any("缺少规模锚点" in c for c in conflicts)

    s2 = _spec({"scale_factor": {"factors": {"users": 2.0}}})
    conflicts2 = validate_planning_targets(s2, has_production_baseline=False)
    assert any("缺少生产基线行数" in c for c in conflicts2)
    assert validate_planning_targets(s2, has_production_baseline=True) == []

    s3 = _spec({})
    assert validate_planning_targets(s3) == ["未提供任何规划目标：5 类目标"
                                             "（rows/scale_factor/storage/"
                                             "workload/auto）至少需其一"]


def test_check_target_combination_alias():
    s = _spec({"rows": {"orders": 1_000_000}})
    assert check_target_combination(s) == []
