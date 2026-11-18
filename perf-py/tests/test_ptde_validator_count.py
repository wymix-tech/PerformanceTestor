"""T6.1 数量验证测试（实施方案 §9.2 T6.1 / §9.5 门禁 1）。

fake 连接沿用 B5 marker 路由口径（勿另起一套）；
路由含参数级匹配（同 SQL 不同 params 需区分）。
"""

from __future__ import annotations

import pytest

from ptde.models.dataset import LayerKind, ValidationStatus
from ptde.profiler.collector import StatisticsCollectionError
from ptde.validator import readonly_scalar
from ptde.validator.count_validator import (
    APPROX_SOURCES,
    EXACT_SOURCES,
    CountTolerance,
    validate_counts,
)


class FakeCursor:
    def __init__(self, routes, executed):
        self._routes = routes
        self._executed = executed
        self._rows: list[tuple] = []

    def execute(self, sql, params=None):
        self._executed.append((sql, list(params or [])))
        for marker, rows, rparams in self._routes:
            if marker in sql and (rparams is None or rparams == list(params or [])):
                self._rows = [tuple(r) for r in rows]
                return
        raise AssertionError(f"未路由 SQL: {sql!r} params={list(params or [])}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakeConn:
    def __init__(self, routes):
        self.routes = routes
        self.executed: list[tuple] = []

    def cursor(self):
        return FakeCursor(self.routes, self.executed)


def _count_route(table: str, n: int):
    return (f"FROM {table}", [(n,)], None)


def test_exact_source_exact_match_passes():
    conn = FakeConn([_count_route("users", 1000)])
    layer = validate_counts(conn, {"users": 1000})
    assert layer.layer is LayerKind.COUNT
    assert layer.status is ValidationStatus.PASS
    assert layer.metrics["exact_tables"] == 1
    assert any("全表精确核对" in e for e in layer.evidence)


def test_exact_source_mismatch_fails_with_delta():
    conn = FakeConn([_count_route("users", 998)])
    layer = validate_counts(conn, {"users": 1000})
    assert layer.status is ValidationStatus.FAIL
    check = layer.checks[0]
    assert "目标 1000，实际 998，偏差 -2" in check.detail


def test_approx_source_within_tolerance_is_warn_not_pass():
    """统计容差内偏差 → WARN（可降级项），不伪装 PASS。"""
    conn = FakeConn([_count_route("users", 1010)])
    layer = validate_counts(
        conn, {"users": 1000}, sources={"users": "estimated"},
        tolerance=CountTolerance(relative=0.02),
    )
    assert layer.status is ValidationStatus.WARN
    assert layer.metrics["tolerated_tables"] == 1
    assert "统计目标" in layer.checks[0].detail


def test_approx_source_default_tolerance_zero_is_exact():
    """容差默认 0（无通用固定值）——未显式配置即逐行精确。"""
    conn = FakeConn([_count_route("users", 1010)])
    layer = validate_counts(conn, {"users": 1000}, sources={"users": "estimated"})
    assert layer.status is ValidationStatus.FAIL


def test_approx_source_beyond_tolerance_fails():
    conn = FakeConn([_count_route("users", 1100)])
    layer = validate_counts(
        conn, {"users": 1000}, sources={"users": "estimated"},
        tolerance=CountTolerance(relative=0.02),
    )
    assert layer.status is ValidationStatus.FAIL


def test_unknown_source_fails_explicitly():
    conn = FakeConn([_count_route("users", 1000)])
    layer = validate_counts(conn, {"users": 1000}, sources={"users": "alien"})
    assert layer.status is ValidationStatus.FAIL
    assert "不在已知来源集合" in layer.checks[0].detail


def test_count_unavailable_fails_not_silent():
    conn = FakeConn([("FROM users", [], None)])
    layer = validate_counts(conn, {"users": 1000})
    assert layer.status is ValidationStatus.FAIL
    assert "不可得" in layer.checks[0].detail


def test_shard_sum_mismatch_fails():
    """分片之和 ≠ 实际行数 → FAIL（数量复核锚点，门禁 1）。"""
    conn = FakeConn([_count_route("users", 1000)])
    layer = validate_counts(conn, {"users": 1000}, shard_loaded={"users": 999})
    assert layer.status is ValidationStatus.FAIL
    assert any(c.name == "shard_sum:users" for c in layer.checks)


def test_shard_sum_equal_passes():
    conn = FakeConn([_count_route("users", 1000)])
    layer = validate_counts(conn, {"users": 1000}, shard_loaded={"users": 1000})
    assert layer.status is ValidationStatus.PASS
    assert any("全量非抽样核对" in c.detail for c in layer.checks)


def test_multiple_tables_sorted_deterministic():
    routes = [_count_route("users", 10), _count_route("orders", 20)]
    conn = FakeConn(routes)
    layer = validate_counts(conn, {"users": 10, "orders": 20})
    assert layer.status is ValidationStatus.PASS
    names = [c.name for c in layer.checks]
    assert names == ["count:orders", "count:users"]
    assert layer.metrics["rows_expected"] == 30
    assert layer.metrics["rows_actual"] == 30


def test_invalid_identifier_rejected():
    conn = FakeConn([])
    with pytest.raises(StatisticsCollectionError):
        validate_counts(conn, {"users; DROP TABLE x": 10})


def test_readonly_scalar_enforces_whitelist():
    """R2 机制延续：非 SELECT 白名单外直接拒绝。"""
    conn = FakeConn([])
    with pytest.raises(StatisticsCollectionError):
        readonly_scalar(conn, "DELETE FROM users")


def test_source_sets_align_with_row_source():
    assert EXACT_SOURCES == {"explicit", "derived"}
    assert APPROX_SOURCES == {"production", "estimated"}
