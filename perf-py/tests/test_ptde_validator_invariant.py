"""T6.4 业务不变量验证测试（实施方案 §9.2 T6.4；总方案 §5.3）。"""

from __future__ import annotations

from ptde.models.base import EvidenceStatus
from ptde.models.dataset import ValidationStatus
from ptde.models.entity import BusinessInvariant, InvariantPredicate
from ptde.validator.invariant_validator import validate_invariants


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


def _inv(id_, status=EvidenceStatus.CONFIRMED, pred=None):
    return BusinessInvariant(id=id_, description=id_, status=status, predicate=pred)


def _relation_pred():
    return InvariantPredicate(
        type="relation_exists",
        when={"table": "orders", "column": "user_id"},
        require={"table": "users", "column": "id"},
    )


def test_confirmed_relation_exists_zero_orphan_passes():
    conn = FakeConn([("LEFT JOIN users", [(0,)], None)])
    layer = validate_invariants(conn, [_inv("I1", pred=_relation_pred())])
    assert layer.status is ValidationStatus.PASS
    assert layer.metrics["executed"] == 1
    assert "非抽样强完整性证据" in layer.checks[0].detail


def test_confirmed_relation_exists_violation_fails():
    conn = FakeConn([("LEFT JOIN users", [(5,)], None)])
    layer = validate_invariants(conn, [_inv("I1", pred=_relation_pred())])
    assert layer.status is ValidationStatus.FAIL
    assert "发现 5 条孤儿" in layer.checks[0].detail


def test_inferred_invariant_skipped_not_enforced():
    """AI 推断不作为硬规则：跳过执行（WARN），不参与 READY 硬门禁。"""
    conn = FakeConn([("LEFT JOIN users", [(99,)], None)])
    layer = validate_invariants(conn, [
        _inv("I2", status=EvidenceStatus.INFERRED, pred=_relation_pred()),
        _inv("I3", status=EvidenceStatus.UNKNOWN, pred=_relation_pred()),
    ])
    assert layer.status is ValidationStatus.WARN
    assert layer.metrics["skipped"] == 2
    assert layer.metrics["executed"] == 0
    assert all("不作为硬规则" in c.detail for c in layer.checks)


def test_confirmed_without_predicate_fails():
    """已确认不变量缺谓词 → FAIL（不伪装通过）。"""
    conn = FakeConn([])
    layer = validate_invariants(conn, [_inv("I4")])
    assert layer.status is ValidationStatus.FAIL
    assert "缺少结构化谓词" in layer.checks[0].detail


def test_unknown_predicate_type_fails():
    conn = FakeConn([])
    inv = _inv("I5", pred=InvariantPredicate(type="quantum_consistency"))
    layer = validate_invariants(conn, [inv])
    assert layer.status is ValidationStatus.FAIL
    assert "不可编译" in layer.checks[0].detail


def test_relation_exists_missing_keys_fails():
    conn = FakeConn([])
    inv = _inv("I6", pred=InvariantPredicate(type="relation_exists",
                                             when={"table": "orders"}))
    layer = validate_invariants(conn, [inv])
    assert layer.status is ValidationStatus.FAIL
    assert "编译失败" in layer.checks[0].detail


def test_identifier_whitelist_rejects_injection():
    conn = FakeConn([])
    inv = _inv("I7", pred=InvariantPredicate(
        type="not_null", when={"table": "users", "column": "id; DROP TABLE x"}))
    layer = validate_invariants(conn, [inv])
    assert layer.status is ValidationStatus.FAIL
    assert "编译失败" in layer.checks[0].detail


def test_not_null_pass_and_fail():
    conn = FakeConn([("WHERE email IS NULL", [(0,)], None)])
    inv = _inv("I8", pred=InvariantPredicate(
        type="not_null", when={"table": "users", "column": "email"}))
    layer = validate_invariants(conn, [inv])
    assert layer.status is ValidationStatus.PASS

    conn_bad = FakeConn([("WHERE email IS NULL", [(7,)], None)])
    layer_bad = validate_invariants(conn_bad, [inv])
    assert layer_bad.status is ValidationStatus.FAIL
    assert "发现 7 条 NULL" in layer_bad.checks[0].detail


def test_sorted_deterministic_output():
    conn = FakeConn([("LEFT JOIN users", [(0,)], None)])
    layer = validate_invariants(conn, [
        _inv("B2", pred=_relation_pred()), _inv("A1", pred=_relation_pred())])
    assert [c.name for c in layer.checks] == ["invariant:A1", "invariant:B2"]
