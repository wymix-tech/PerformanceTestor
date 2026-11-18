"""T3.2 验收：约束求解与显式失败（PlanningConflict 逐条冲突；禁止静默收缩，A1）。"""

from __future__ import annotations

import pytest

from ptde.models.spec import ResourceConstraints, StorageTarget
from ptde.planner.solver import (
    SOLVER_ALGORITHM_VERSION,
    PlanningConflict,
    SolverSettings,
    check_constraints,
    solve,
)

GB = 1024**3


def test_solve_success():
    rows = {"orders": 5_000_000, "users": 400_000}
    result = solve(rows, 3 * GB, ResourceConstraints(max_staging_storage_bytes=10 * GB))
    assert result.total_rows == 5_400_000
    assert result.estimated_storage_bytes == 3 * GB
    assert result.algorithm_version == SOLVER_ALGORITHM_VERSION
    assert result.conflicts == []


def test_storage_conflict_lists_each_violation():
    """1 亿订单 vs 200GB：超出 staging 约束与存储目标两条冲突各自成条。"""
    rows = {"orders": 100_000_000}
    storage = 300 * GB
    constraints = ResourceConstraints(max_staging_storage_bytes=250 * GB)
    conflicts = check_constraints(
        rows, storage, constraints, StorageTarget(max_total="200GB")
    )
    assert len(conflicts) == 2
    assert any("max_staging_storage" in c for c in conflicts)
    assert any("存储目标" in c and "200GB" in c for c in conflicts)


def test_planning_conflict_raises_with_details():
    rows = {"orders": 100_000_000}
    with pytest.raises(PlanningConflict) as exc:
        solve(rows, 300 * GB, ResourceConstraints(max_staging_storage_bytes=250 * GB),
              StorageTarget(max_total="200GB"))
    assert len(exc.value.conflicts) == 2
    msg = str(exc.value)
    assert "2 条冲突" in msg
    assert "max_staging_storage" in msg and "200GB" in msg


def test_no_silent_shrink_on_failure():
    """不可满足时输入行数不被修改（A1：新管线无钳制、禁止静默降低行数）。"""
    rows = {"orders": 100_000_000}
    snapshot = dict(rows)
    with pytest.raises(PlanningConflict):
        solve(rows, 300 * GB, ResourceConstraints(max_staging_storage_bytes=1 * GB))
    assert rows == snapshot


def test_generation_time_conflict():
    rows = {"orders": 10_000_000}
    constraints = ResourceConstraints(max_generation_time_ms=50_000)
    settings = SolverSettings(generation_throughput_rows_per_sec=100_000.0)
    conflicts = check_constraints(rows, 0, constraints, None, settings)
    assert len(conflicts) == 1 and "max_generation_time" in conflicts[0]
    # 10M 行 @ 100k 行/秒 = 100 秒 = 100000ms > 50000ms
    result = solve(rows, 0, ResourceConstraints(), None, settings)
    assert result.estimated_generation_time_ms == pytest.approx(100_000.0)


def test_connection_constraint_conflict():
    rows = {"t": 100}
    constraints = ResourceConstraints(max_database_connections=2)
    conflicts = check_constraints(
        rows, 0, constraints, None, SolverSettings(planned_loader_connections=4)
    )
    assert len(conflicts) == 1 and "max_database_connections" in conflicts[0]


def test_constraints_all_pass():
    rows = {"t": 1_000_000}
    constraints = ResourceConstraints(
        max_staging_storage_bytes=10 * GB,
        max_generation_time_ms=3_600_000,
        max_database_connections=16,
    )
    assert check_constraints(rows, 1 * GB, constraints) == []
