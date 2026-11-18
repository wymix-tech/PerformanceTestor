"""T6.2 结构关系验证测试（实施方案 §9.2 T6.2 / §9.5 门禁 1、3）。

marker 路由注意包含冲突（B5 §4 第 4 条延续）：
更具体的 marker 放前面（first-match 语义）。
"""

from __future__ import annotations

import pytest

from ptde.models.dataset import ValidationStatus
from ptde.models.entity import Cardinality, RelationKind, RelationModel
from ptde.profiler.collector import StatisticsCollectionError
from ptde.validator.relationship_validator import validate_relationships


class FakeCursor:
    def __init__(self, routes, executed):
        self._routes = routes
        self._executed = executed
        self._rows: list[tuple] = []

    def execute(self, sql, params=None):
        self._executed.append((sql, list(params or [])))
        for route in self._routes:
            marker, rows = route[0], route[1]
            if marker in sql:
                self._rows = [tuple(r) for r in rows]
                return
        raise AssertionError(f"未路由 SQL: {sql!r}")

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


def _fk(child, col, parent, pcol, kind=RelationKind.DB_FK):
    return RelationModel(
        child_table=child, child_column=col,
        parent_table=parent, parent_column=pcol,
        cardinality=Cardinality.MANY_TO_ONE, relation_kind=kind,
    )


def test_zero_orphan_fk_passes_with_strong_evidence():
    """零孤儿全量核对（非抽样）→ 强完整性证据入 evidence（T6.2 原文）。"""
    conn = FakeConn([("LEFT JOIN users", [(0,)], None)])
    layer = validate_relationships(conn, [_fk("orders", "user_id", "users", "id")])
    assert layer.status is ValidationStatus.PASS
    assert any("非抽样" in e and "零孤儿" in e for e in layer.evidence)


def test_declared_fk_orphans_fail():
    conn = FakeConn([("LEFT JOIN users", [(3,)], None)])
    layer = validate_relationships(conn, [_fk("orders", "user_id", "users", "id")])
    assert layer.status is ValidationStatus.FAIL
    assert "发现 3 条孤儿" in layer.checks[0].detail


def test_logical_relation_orphans_degrade_to_warn():
    """LOGICAL 推断关系孤儿 → WARN（不作为硬规则，A3）。"""
    conn = FakeConn([("LEFT JOIN users", [(2,)], None)])
    layer = validate_relationships(
        conn, [_fk("orders", "user_id", "users", "id", RelationKind.LOGICAL)])
    assert layer.status is ValidationStatus.WARN
    assert "推断" in layer.checks[0].detail


def test_incomplete_relation_fails():
    conn = FakeConn([])
    layer = validate_relationships(conn, [
        RelationModel(child_table="orders", child_column="",
                      parent_table="users", parent_column="id")])
    assert layer.status is ValidationStatus.FAIL
    assert "关系定义不完整" in layer.checks[0].detail


def test_duplicate_quads_checked_once():
    calls = []

    class CountingConn(FakeConn):
        def cursor(self):
            calls.append(1)
            return FakeCursor([("LEFT JOIN users", [(0,)], None)], [])

    conn = CountingConn([])
    layer = validate_relationships(conn, [
        _fk("orders", "user_id", "users", "id"),
        _fk("orders", "user_id", "users", "id"),
    ])
    assert layer.status is ValidationStatus.PASS
    assert layer.metrics["relations_checked"] == 1


def test_pk_integrity_null_or_duplicate_fails():
    """COUNT(*) vs COUNT(DISTINCT)：NULL 与重复一网打尽。"""
    conn = FakeConn([
        ("COUNT(DISTINCT id)", [(98,)], None),
        ("FROM users", [(100,)], None),
    ])
    layer = validate_relationships(conn, [], pk_keys={"users": ["id"]})
    assert layer.status is ValidationStatus.FAIL
    assert "NULL 或重复" in layer.checks[0].detail


def test_pk_integrity_pass():
    conn = FakeConn([
        ("COUNT(DISTINCT id)", [(100,)], None),
        ("FROM users", [(100,)], None),
    ])
    layer = validate_relationships(conn, [], pk_keys={"users": ["id"]})
    assert layer.status is ValidationStatus.PASS


def test_unique_key_violation_fails():
    conn = FakeConn([
        ("COUNT(DISTINCT email)", [(99,)], None),
        ("FROM users", [(100,)], None),
    ])
    layer = validate_relationships(conn, [], unique_keys={"users": ["email"]})
    assert layer.status is ValidationStatus.FAIL


def test_mn_join_duplicate_pairs_fail():
    conn = FakeConn([
        ("COUNT(DISTINCT user_id, role_id)", [(100,)], None),
        ("FROM user_role", [(120,)], None),
    ])
    layer = validate_relationships(
        conn, [], join_tables={"user_role": ("user_id", "role_id")})
    assert layer.status is ValidationStatus.FAIL
    assert "M:N 联结对重复" in layer.checks[0].detail


def test_mn_join_unique_pairs_pass():
    conn = FakeConn([
        ("COUNT(DISTINCT user_id, role_id)", [(100,)], None),
        ("FROM user_role", [(100,)], None),
    ])
    layer = validate_relationships(
        conn, [], join_tables={"user_role": ("user_id", "role_id")})
    assert layer.status is ValidationStatus.PASS


def test_cardinality_violations_fail():
    conn = FakeConn([("HAVING COUNT(*) > 10", [(3,)], None)])
    layer = validate_relationships(
        conn, [], cardinality_limits={"orders.user_id": 10})
    assert layer.status is ValidationStatus.FAIL
    assert "超过上限 10" in layer.checks[0].detail


def test_cardinality_within_limit_passes():
    conn = FakeConn([("HAVING COUNT(*) > 10", [(0,)], None)])
    layer = validate_relationships(
        conn, [], cardinality_limits={"orders.user_id": 10})
    assert layer.status is ValidationStatus.PASS


def test_identifier_whitelist_enforced():
    conn = FakeConn([])
    with pytest.raises(StatisticsCollectionError):
        validate_relationships(conn, [_fk("orders", "user_id", "users; --", "id")])


def test_empty_inputs_all_pass_with_metrics_zero():
    conn = FakeConn([])
    layer = validate_relationships(conn, [])
    assert layer.status is ValidationStatus.PASS
    assert layer.metrics["relations_checked"] == 0
