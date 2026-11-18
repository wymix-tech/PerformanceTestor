"""T5.9 执行前安全门禁测试（实施方案 §8.2/§8.4；总方案 §13.1/§13.2/§13.3）。

覆盖：§8.4 检查项逐项（环境识别不得仅凭连接名/主机名/环境变量名——凭
Capability Probe 实测；生产库/无法识别环境禁止写入；凭据分离 A5；资源
预算不伪装通过；外部副作用默认隔离；破坏性操作显式授权+审计）。

鸭子类型假连接（探测路由复用 T5.2 测试口径，勿另起一套）。
"""

from __future__ import annotations

import pytest

from ptde.adapters.probe import CAPABILITY_KEYS
from ptde.loader.gate import (
    DESTRUCTIVE_OPERATIONS,
    AuditLog,
    DestructiveApproval,
    EnvClass,
    GateDenied,
    SecurityGate,
)


class FakeCursor:
    """按 SQL 关键字路由返回行的假游标（复用 T5.2 探测测试形态）。"""

    def __init__(self, routes: dict[str, list[tuple]]) -> None:
        self._routes = routes
        self._result: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
        self._result = []
        for marker, rows in self._routes.items():
            if marker in sql:
                self._result = rows
                return

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def close(self) -> None:
        pass


class FakeConn:
    def __init__(self, routes: dict[str, list[tuple]]) -> None:
        self._routes = routes

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._routes)


# 探测路由（对齐 tests/test_ptde_adapters_probe.py 的 MYSQL_ROUTES 口径）
MYSQL_PROBE_ROUTES = {
    "SELECT VERSION()": [("8.0.36",)],
    "version_comment": [("MySQL Community Server - GPL",)],
    "default_storage_engine": [("InnoDB",)],
    "SELECT @@sql_mode": [("STRICT_TRANS_TABLES",)],
    "SELECT @@local_infile": [("1",)],
    "SELECT @@secure_file_priv": [("/var/lib/mysql-files/",)],
    "SELECT @@max_allowed_packet": [(67108864,)],
    "FROM information_schema.TABLES WHERE TABLE_SCHEMA": [(5,)],
    "information_schema.STATISTICS": [(8,)],
    "KEY_COLUMN_USAGE": [(2,)],
    "information_schema.PARTITIONS": [(3,)],
    "information_schema.TRIGGERS": [(1,)],
    "EXTRA LIKE": [(1,)],
    "DATA_TYPE IN": [(4,)],
}


def _gate() -> SecurityGate:
    return SecurityGate(audit=AuditLog())


def _evaluate(conn, *, env="dedicated_test_db", **overrides) -> object:
    params = dict(
        target_dsn="mysql://loader@target/db",
        profile_dsn="mysql://collector@profile/db",
        declared_env_class=env,
        permission_ok=True,
        trigger_count=0,
        schema_isolation_confirmed=True,
    )
    params.update(overrides)
    gate = _gate()
    return gate.evaluate_write(conn, "mysql", **params)


# ============================================================ 环境识别（§13.1）


def test_unrecognized_environment_denied() -> None:
    """探测失败（版本不可得）→ 无法识别的环境禁止写入（§13.1）。"""
    gate = _gate()
    report = gate.evaluate_write(
        FakeConn({}), "mysql",  # 空路由：VERSION() 不可得
        target_dsn="mysql://a", profile_dsn="mysql://b",
        declared_env_class="dedicated_test_db",
    )
    check = next(c for c in report.checks if c.name == "environment_recognition")
    assert check.passed is False
    assert "无法识别" in check.detail
    assert "连接名" in check.detail  # 不得仅凭连接名/主机名/环境变量名（§13.1）
    assert gate.audit.events[0]["event_type"] == "gate_denied"
    with pytest.raises(GateDenied, match="environment_recognition"):
        report.require_passed()


def test_unregistered_product_denied() -> None:
    report = _gate().evaluate_write(
        FakeConn(MYSQL_PROBE_ROUTES), "oracle",
        target_dsn="a", profile_dsn="b",
        declared_env_class="dedicated_test_db",
    )
    with pytest.raises(GateDenied):
        report.require_passed()


def test_recognition_uses_probe_snapshot_not_names() -> None:
    report = _evaluate(FakeConn(MYSQL_PROBE_ROUTES))
    check = next(c for c in report.checks if c.name == "environment_recognition")
    assert check.passed is True
    assert "Capability Probe" in check.detail
    assert report.db_version == "8.0.36"
    assert report.capability_snapshot is not None
    assert set(CAPABILITY_KEYS) <= set(report.capability_snapshot["capabilities"])


# ============================================================ 环境策略（§13.1 表）


def test_production_write_denied_and_audited() -> None:
    gate = _gate()
    report = gate.evaluate_write(
        FakeConn(MYSQL_PROBE_ROUTES), "mysql",
        target_dsn="mysql://a", profile_dsn="mysql://b",
        declared_env_class="production",
        permission_ok=True, trigger_count=0,
        schema_isolation_confirmed=True,
    )
    check = next(c for c in report.checks if c.name == "environment_policy")
    assert check.passed is False
    assert "生产数据库禁止" in check.detail
    assert gate.audit.events[-1]["event_type"] == "gate_denied"
    with pytest.raises(GateDenied, match="environment_policy"):
        report.require_passed()


def test_unknown_env_class_denied() -> None:
    report = gate_report = _gate().evaluate_write(
        FakeConn(MYSQL_PROBE_ROUTES), "mysql",
        target_dsn="mysql://a", profile_dsn="mysql://b",
        declared_env_class="unknown",
        permission_ok=True, trigger_count=0,
    )
    del gate_report
    with pytest.raises(GateDenied, match="environment_policy"):
        report.require_passed()


def test_shared_test_requires_isolation_confirmation() -> None:
    base = dict(
        conn=FakeConn(MYSQL_PROBE_ROUTES), product="mysql",
        target_dsn="mysql://a", profile_dsn="mysql://b",
        declared_env_class="shared_test",
        permission_ok=True, trigger_count=0,
    )
    gate = _gate()
    report = gate.evaluate_write(**base)
    with pytest.raises(GateDenied, match="共享"):
        report.require_passed()
    report = gate.evaluate_write(**base, schema_isolation_confirmed=True)
    assert report.passed_all() is False  # 资源预算未执行仍拦（见下）


# ============================================================ 权限 / 预算 / 副作用


def test_permission_unmeasured_rejected() -> None:
    report = _evaluate(FakeConn(MYSQL_PROBE_ROUTES), permission_ok=None)
    check = next(c for c in report.checks if c.name == "write_privilege")
    assert check.passed is False
    assert "未实测" in check.detail


def test_permission_insufficient_rejected() -> None:
    report = _evaluate(FakeConn(MYSQL_PROBE_ROUTES), permission_ok=False)
    with pytest.raises(GateDenied, match="write_privilege"):
        report.require_passed()


def test_budget_probe_missing_not_silently_passed() -> None:
    report = _evaluate(FakeConn(MYSQL_PROBE_ROUTES))  # 未提供 budget_probe
    check = next(c for c in report.checks if c.name == "resource_budget")
    assert check.passed is False
    assert check.not_executed
    assert "[未执行]" in check.detail


def test_budget_probe_measured_passes() -> None:
    report = _evaluate(
        FakeConn(MYSQL_PROBE_ROUTES),
        budget_probe=lambda: {"disk_free_bytes": 1 << 40, "log_free_bytes": 1 << 33},
    )
    check = next(c for c in report.checks if c.name == "resource_budget")
    assert check.passed is True
    assert "disk_free_bytes" in check.detail


def test_budget_probe_failure_rejected() -> None:
    def _boom():
        raise RuntimeError("df 不可用")

    report = _evaluate(FakeConn(MYSQL_PROBE_ROUTES), budget_probe=_boom)
    check = next(c for c in report.checks if c.name == "resource_budget")
    assert check.passed is False and "df 不可用" in check.detail


def test_trigger_side_effects_require_isolation() -> None:
    report = _evaluate(
        FakeConn(MYSQL_PROBE_ROUTES), trigger_count=2,
        side_effect_isolation_confirmed=False,
    )
    check = next(c for c in report.checks if c.name == "external_side_effects")
    assert check.passed is False
    assert "默认隔离" in check.detail
    ok = _evaluate(
        FakeConn(MYSQL_PROBE_ROUTES), trigger_count=2,
        side_effect_isolation_confirmed=True,
    )
    assert next(c for c in ok.checks if c.name == "external_side_effects").passed


# ============================================================ 破坏性操作（授权+审计）


def test_destructive_without_approval_rejected() -> None:
    report = _evaluate(
        FakeConn(MYSQL_PROBE_ROUTES),
        budget_probe=lambda: {},
        requested_destructive=["TRUNCATE"],
    )
    check = next(c for c in report.checks if c.name == "destructive_operations")
    assert check.passed is False


def test_destructive_with_approval_granted_and_audited() -> None:
    gate = _gate()
    approval = DestructiveApproval(
        operation="TRUNCATE", target="orders",
        authorized_by="dba_zhang", ticket="CHG-123",
    )
    report = gate.evaluate_write(
        FakeConn(MYSQL_PROBE_ROUTES), "mysql",
        target_dsn="mysql://a", profile_dsn="mysql://b",
        declared_env_class="dedicated_test_db",
        permission_ok=True, trigger_count=0,
        schema_isolation_confirmed=True,
        budget_probe=lambda: {},
        approvals=[approval],
        requested_destructive=["TRUNCATE"],
    )
    check = next(c for c in report.checks if c.name == "destructive_operations")
    assert check.passed is True
    granted = [e for e in gate.audit.events if e["event_type"] == "destructive_granted"]
    assert len(granted) == 1
    assert granted[0]["operation"] == "TRUNCATE"
    assert granted[0]["authorized_by"] == "dba_zhang"
    assert granted[0]["ticket"] == "CHG-123"


def test_destructive_approval_invalid_operation_rejected() -> None:
    with pytest.raises(ValueError, match="授权范围"):
        DestructiveApproval(operation="DELETE_ALL", target="x",
                            authorized_by="a")


def test_destructive_approval_empty_authorizer_rejected() -> None:
    with pytest.raises(ValueError, match="authorized_by"):
        DestructiveApproval(operation="TRUNCATE", target="x", authorized_by=" ")


def test_destructive_operations_domain_locked() -> None:
    assert DESTRUCTIVE_OPERATIONS == ("DDL", "TRUNCATE", "DROP", "CLEANUP")


# ============================================================ 凭据分离（A5）


def test_same_dsn_rejected() -> None:
    gate = _gate()
    report = gate.evaluate_write(
        FakeConn(MYSQL_PROBE_ROUTES), "mysql",
        target_dsn="mysql://same", profile_dsn="mysql://same",
        declared_env_class="dedicated_test_db",
        permission_ok=True, trigger_count=0,
    )
    check = next(c for c in report.checks if c.name == "credential_separation")
    assert check.passed is False
    assert "A5" in check.detail


def test_empty_dsn_rejected() -> None:
    report = _evaluate(FakeConn(MYSQL_PROBE_ROUTES),
                       target_dsn="mysql://a", profile_dsn="")
    check = next(c for c in report.checks if c.name == "credential_separation")
    assert check.passed is False


# ============================================================ 全过路径与清理门禁


def test_full_pass_allowed_and_audited() -> None:
    gate = _gate()
    report = gate.evaluate_write(
        FakeConn(MYSQL_PROBE_ROUTES), "mysql",
        target_dsn="mysql://loader@t", profile_dsn="mysql://collector@p",
        declared_env_class=EnvClass.DEDICATED_TEST_DB,
        permission_ok=True, trigger_count=0,
        schema_isolation_confirmed=True,
        budget_probe=lambda: {"disk_free_bytes": 1 << 40},
    )
    assert report.passed_all()
    report.require_passed()
    assert [e["event_type"] for e in gate.audit.events] == ["gate_allowed"]
    assert report.declared_env_class == "dedicated_test_db"


def test_cleanup_production_denied() -> None:
    report = _gate().evaluate_cleanup(declared_env_class="production")
    with pytest.raises(GateDenied, match="生产"):
        report.require_passed()


def test_cleanup_requires_destructive_approval() -> None:
    report = _gate().evaluate_cleanup(declared_env_class="dedicated_test_db")
    with pytest.raises(GateDenied, match="CLEANUP"):
        report.require_passed()


def test_cleanup_with_approval_ok_and_audited() -> None:
    gate = _gate()
    approval = DestructiveApproval(operation="CLEANUP", target="ptde_ds",
                                   authorized_by="dba_li", ticket="CHG-9")
    report = gate.evaluate_cleanup(
        declared_env_class="dedicated_test_db", target_dsn="mysql://a",
        approvals=[approval],
    )
    assert report.passed_all()
    assert [e["event_type"] for e in gate.audit.events] == ["destructive_granted",
                                                            "gate_allowed"]
