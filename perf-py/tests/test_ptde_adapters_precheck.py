"""T5.6 装载前目标表检查测试（实施方案 §8.2；总方案 §9.4 逐项）。

覆盖：§9.4 检查项全集（触发器/生成列/约束/分区/LOB/自增/RLS/复制限制）、
默认保留全部约束口径、blocked 项显式拒绝（require_loadable）、未知产品
显式失败。鸭子类型假连接（marker 路由防包含冲突——B5 §4 第 4 条）。
"""

from __future__ import annotations

import pytest

from ptde.adapters.precheck import (
    DEFAULT_KEEP_CONSTRAINTS_NOTE,
    MYSQL_PRECHECK_ROUTES,
    PG_PRECHECK_ROUTES,
    PrecheckFinding,
    PrecheckReport,
    PrecheckStatus,
    precheck_table,
)


class RouteCursor:
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


class RouteConn:
    def __init__(self, routes: dict[str, list[tuple]]) -> None:
        self._routes = routes

    def cursor(self) -> RouteCursor:
        return RouteCursor(self._routes)


def _items(report: PrecheckReport) -> list[str]:
    return [f.item for f in report.findings]


# ============================================================ §9.4 检查项全集


def test_mysql_precheck_covers_all_items_with_notes() -> None:
    report = precheck_table(RouteConn(MYSQL_PRECHECK_ROUTES), "mysql", "orders")
    assert _items(report) == [
        "triggers", "generated_columns", "constraints", "partitioning",
        "lob_json_types", "auto_increment_sequence", "rls", "replication_limits",
    ]
    triggers = report.finding("triggers")
    assert triggers.status == PrecheckStatus.NOTE.value
    assert "trg_audit" in triggers.detail and "外部副作用" in triggers.detail
    assert report.finding("generated_columns").status == PrecheckStatus.NOTE.value
    assert "不可直接装载" in report.finding("generated_columns").detail
    assert "默认保留全部约束" in report.finding("constraints").detail
    assert report.finding("partitioning").status == PrecheckStatus.NOTE.value
    assert "json" in report.finding("lob_json_types").detail
    assert "AUTO_INCREMENT" in report.finding("auto_increment_sequence").detail
    assert report.finding("rls").status == PrecheckStatus.OK.value  # MySQL 不适用
    assert not report.is_blocked()
    assert DEFAULT_KEEP_CONSTRAINTS_NOTE in report.notes


def test_mysql_read_only_blocked() -> None:
    routes = {**MYSQL_PRECHECK_ROUTES, "SELECT @@read_only": [(1,)]}
    report = precheck_table(RouteConn(routes), "mysql", "orders")
    assert report.finding("replication_limits").blocked()
    assert "read_only" in report.finding("replication_limits").detail
    with pytest.raises(ValueError, match="预检不通过"):
        report.require_loadable()


def test_mysql_super_read_only_blocked() -> None:
    routes = {**MYSQL_PRECHECK_ROUTES, "SELECT @@super_read_only": [(1,)]}
    report = precheck_table(RouteConn(routes), "mysql", "orders")
    assert report.is_blocked()
    assert "super_read_only" in report.finding("replication_limits").detail


def test_pg_precheck_covers_all_items_ok() -> None:
    report = precheck_table(RouteConn(PG_PRECHECK_ROUTES), "postgres", "orders")
    assert _items(report) == [
        "triggers", "generated_columns", "constraints", "partitioning",
        "lob_json_types", "auto_increment_sequence", "rls", "replication_limits",
    ]
    assert not report.is_blocked()
    assert "RLS 未启用" in report.finding("rls").detail


def test_pg_rls_blocked_refuses_load() -> None:
    routes = {**PG_PRECHECK_ROUTES, "relrowsecurity": [(True, True)]}
    report = precheck_table(RouteConn(routes), "postgres", "orders")
    assert report.finding("rls").blocked()
    assert "RLS 策略豁免" in report.finding("rls").detail  # T5.4 口径：处置建议
    with pytest.raises(ValueError, match="blocked"):
        report.require_loadable()


def test_pg_standby_blocked() -> None:
    routes = {**PG_PRECHECK_ROUTES, "pg_is_in_recovery()": [(True,)]}
    report = precheck_table(RouteConn(routes), "postgres", "orders")
    assert report.is_blocked()
    assert "备库" in report.finding("replication_limits").detail


def test_pg_generated_columns_note() -> None:
    routes = {**PG_PRECHECK_ROUTES, "attgenerated <> ''": [("total",)]}
    report = precheck_table(RouteConn(routes), "postgres", "orders")
    assert report.finding("generated_columns").status == PrecheckStatus.NOTE.value
    assert "排除" in report.finding("generated_columns").detail


def test_pg_partition_parent_note() -> None:
    routes = {**PG_PRECHECK_ROUTES, "relkind IN ('p', 'r')": [("p",)]}
    report = precheck_table(RouteConn(routes), "postgres", "orders")
    assert report.finding("partitioning").status == PrecheckStatus.NOTE.value


# ============================================================ 分发与结构


def test_unknown_product_fails() -> None:
    with pytest.raises(ValueError, match="无目标表预检器"):
        precheck_table(RouteConn({}), "oracle", "orders")


def test_product_aliases_normalized() -> None:
    report = precheck_table(RouteConn(PG_PRECHECK_ROUTES), "pg", "orders")
    assert report.product == "postgres"


def test_report_missing_item_raises() -> None:
    report = PrecheckReport(product="mysql", table="orders", findings=[])
    with pytest.raises(ValueError, match="缺少"):
        report.finding("triggers")


def test_blocked_items_listing() -> None:
    report = PrecheckReport(
        product="postgres", table="orders",
        findings=[
            PrecheckFinding(item="triggers", detail=""),
            PrecheckFinding(item="rls", status=PrecheckStatus.BLOCKED.value,
                            detail="RLS 开启"),
        ],
        notes=[],
    )
    assert report.blocked_items() == ["rls"]
    assert report.is_blocked()
