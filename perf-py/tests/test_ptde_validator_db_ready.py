"""T6.5 数据库就绪验证测试（实施方案 §9.2 T6.5 / §9.5 门禁 1）。

产品 SQL 分发（MySQL information_schema / PG pg_catalog）、selfCheck 收编
（datagen_service._self_check 口径：0 命中 WARN、从不阻断、未执行 ≠ 通过）。
断言按检查项名称定位（层内多检查项；self_check 默认 WARN 不干扰单项断言）。
"""

from __future__ import annotations

import pytest

from ptde.models.dataset import LayerKind, ValidationStatus
from ptde.models.spec import StorageEstimate
from ptde.validator.db_ready_validator import (
    HotspotCheck,
    SelectivityCheck,
    validate_db_ready,
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


class FakeStorageMetrics:
    def __init__(self, table_bytes=800, index_bytes=200):
        self.table_bytes = table_bytes
        self.index_bytes = index_bytes

    def measure(self, conn, table, include_indexes=True):
        return (self.table_bytes, self.index_bytes, 42)


def _probe_ok():
    return lambda: {"checked": 3, "nonEmpty": 3}


def find(layer, name):
    return next(c for c in layer.checks if c.name == name)


# ------------------------------------------------------------ 索引完整性

def test_mysql_index_exists_pass():
    conn = FakeConn([("information_schema.STATISTICS", [(1,)], ["orders", "idx_user"])])
    layer = validate_db_ready(conn, "mysql",
                              planned_indexes={"orders": ["idx_user"]},
                              endpoint_probe=_probe_ok())
    assert find(layer, "index:orders.idx_user").status is ValidationStatus.PASS
    assert layer.metrics["planned_indexes"] == 1


def test_mysql_index_missing_fails():
    conn = FakeConn([("information_schema.STATISTICS", [(0,)], ["orders", "idx_user"])])
    layer = validate_db_ready(conn, "mysql",
                              planned_indexes={"orders": ["idx_user"]})
    assert find(layer, "index:orders.idx_user").status is ValidationStatus.FAIL
    assert "计划索引缺失" in find(layer, "index:orders.idx_user").detail


def test_postgres_index_dispatch():
    conn = FakeConn([("pg_indexes", [(1,)], ["orders", "idx_user"])])
    layer = validate_db_ready(conn, "postgres",
                              planned_indexes={"orders": ["idx_user"]})
    assert find(layer, "index:orders.idx_user").status is ValidationStatus.PASS


def test_unknown_product_index_fails_explicitly():
    conn = FakeConn([])
    with pytest.raises(ValueError):
        validate_db_ready(conn, "oracle", planned_indexes={"t": ["i"]})


# ------------------------------------------------------------ 分区分布

def test_partitions_match_plan_pass():
    conn = FakeConn([("information_schema.PARTITIONS", [(4,)], ["events"])])
    layer = validate_db_ready(conn, "mysql", partitioned_tables={"events": 4})
    assert find(layer, "partitions:events").status is ValidationStatus.PASS


def test_partitions_mismatch_fails():
    conn = FakeConn([("information_schema.PARTITIONS", [(3,)], ["events"])])
    layer = validate_db_ready(conn, "mysql", partitioned_tables={"events": 4})
    assert find(layer, "partitions:events").status is ValidationStatus.FAIL


def test_no_partition_config_ok_with_note():
    conn = FakeConn([])
    layer = validate_db_ready(conn, "mysql")
    assert find(layer, "partitions").status is ValidationStatus.PASS
    assert "不适用" in find(layer, "partitions").detail


# ------------------------------------------------------------ ANALYZE 状态

def test_analyze_fresh_pass():
    conn = FakeConn([("SELECT UPDATE_TIME", [("2026-11-18", None)], ["users"])])
    layer = validate_db_ready(conn, "mysql", analyzed_tables=["users"])
    assert find(layer, "analyze:users").status is ValidationStatus.PASS


def test_analyze_stale_warns_by_default():
    conn = FakeConn([("SELECT UPDATE_TIME", [(None, None)], ["users"])])
    layer = validate_db_ready(conn, "mysql", analyzed_tables=["users"])
    assert find(layer, "analyze:users").status is ValidationStatus.WARN
    assert "可降级项" in find(layer, "analyze:users").detail


def test_analyze_stale_required_fails():
    conn = FakeConn([("SELECT UPDATE_TIME", [(None, None)], ["users"])])
    layer = validate_db_ready(
        conn, "mysql", analyzed_tables=["users"], analyze_required=True)
    assert find(layer, "analyze:users").status is ValidationStatus.FAIL


def test_analyze_pg_autoanalyze_counts():
    conn = FakeConn([("pg_stat_user_tables", [(None, "2026-11-17")], ["users"])])
    layer = validate_db_ready(conn, "postgres", analyzed_tables=["users"])
    assert find(layer, "analyze:users").status is ValidationStatus.PASS


def test_analyze_no_tables_warns_not_executed():
    conn = FakeConn([])
    layer = validate_db_ready(conn, "mysql")
    assert find(layer, "statistics_updated").status is ValidationStatus.WARN
    assert "[未执行]" in find(layer, "statistics_updated").detail


# ------------------------------------------------------------ 存储

def test_storage_no_config_ok_with_note():
    conn = FakeConn([])
    layer = validate_db_ready(conn, "mysql")
    assert find(layer, "storage_size").status is ValidationStatus.PASS


def test_storage_budget_without_adapter_fails():
    conn = FakeConn([])
    layer = validate_db_ready(
        conn, "mysql",
        storage_budget=StorageEstimate(estimated_total_size_bytes=1000))
    assert find(layer, "storage_size").status is ValidationStatus.FAIL
    assert "不可实测" in find(layer, "storage_size").detail


def test_storage_within_budget_pass():
    conn = FakeConn([])
    layer = validate_db_ready(
        conn, "mysql", storage_metrics=FakeStorageMetrics(800, 200),
        storage_tables=["users"],
        storage_budget=StorageEstimate(estimated_total_size_bytes=1000))
    assert find(layer, "storage_size").status is ValidationStatus.PASS
    assert layer.metrics["measured_storage_bytes"] == 1000


def test_storage_over_budget_fails():
    conn = FakeConn([])
    layer = validate_db_ready(
        conn, "mysql", storage_metrics=FakeStorageMetrics(900, 300),
        storage_tables=["users"],
        storage_budget=StorageEstimate(estimated_total_size_bytes=1000))
    assert find(layer, "storage_size").status is ValidationStatus.FAIL


# ------------------------------------------------------------ 核心查询计划

def test_core_query_explain_pass():
    conn = FakeConn([("EXPLAIN SELECT", [(1, 2)], None)])
    layer = validate_db_ready(
        conn, "mysql", core_queries=["SELECT id FROM users WHERE id = 1"])
    assert find(layer, "explain[0]").status is ValidationStatus.PASS


def test_core_query_empty_plan_fails():
    conn = FakeConn([("EXPLAIN SELECT", [], None)])
    layer = validate_db_ready(
        conn, "mysql", core_queries=["SELECT id FROM users WHERE id = 1"])
    assert find(layer, "explain[0]").status is ValidationStatus.FAIL


def test_core_query_whitelist_enforced():
    """内层语句先过白名单（R2 机制）——非 SELECT 显式失败入检查项。"""
    conn = FakeConn([])
    layer = validate_db_ready(
        conn, "mysql", core_queries=["DELETE FROM users WHERE id = 1"])
    assert find(layer, "explain[0]").status is ValidationStatus.FAIL
    assert "语句白名单" in find(layer, "explain[0]").detail


# ------------------------------------------------------------ 选择性/热点

def test_selectivity_within_tolerance_pass():
    conn = FakeConn([("COUNT(DISTINCT user_id) FROM orders", [(1000, 95)], None)])
    layer = validate_db_ready(
        conn, "mysql",
        selectivity_checks=[SelectivityCheck(
            table="orders", column="user_id", expected=0.1)])
    assert find(layer, "selectivity:orders.user_id").status is ValidationStatus.PASS


def test_selectivity_deviation_fails():
    conn = FakeConn([("COUNT(DISTINCT user_id) FROM orders", [(1000, 900)], None)])
    layer = validate_db_ready(
        conn, "mysql",
        selectivity_checks=[SelectivityCheck(
            table="orders", column="user_id", expected=0.1)])
    assert find(layer, "selectivity:orders.user_id").status is ValidationStatus.FAIL


def test_hotspot_reached_pass():
    conn = FakeConn([
        ("SELECT MAX(c)", [(400,)], None),
        ("IS NOT NULL", [(1000,)], None),
    ])
    layer = validate_db_ready(
        conn, "mysql",
        hotspot_checks=[HotspotCheck(table="orders", column="city_id",
                                     min_ratio=0.3)])
    assert find(layer, "hotspot:orders.city_id").status is ValidationStatus.PASS


def test_hotspot_not_reached_fails():
    conn = FakeConn([
        ("SELECT MAX(c)", [(100,)], None),
        ("IS NOT NULL", [(1000,)], None),
    ])
    layer = validate_db_ready(
        conn, "mysql",
        hotspot_checks=[HotspotCheck(table="orders", column="city_id",
                                     min_ratio=0.3)])
    assert find(layer, "hotspot:orders.city_id").status is ValidationStatus.FAIL


# ------------------------------------------------------------ selfCheck 收编

def test_self_check_no_probe_warns():
    """未提供端点抽检回调 → [未执行]（不伪装通过）。"""
    conn = FakeConn([])
    layer = validate_db_ready(conn, "mysql")
    assert find(layer, "self_check").status is ValidationStatus.WARN
    assert "[未执行]" in find(layer, "self_check").detail


def test_self_check_zero_hit_rate_warns_legacy_semantics():
    """0 命中加 warning（legacy selfCheck 口径：从不阻断）。"""
    conn = FakeConn([])
    layer = validate_db_ready(
        conn, "mysql", endpoint_probe=lambda: {"checked": 3, "nonEmpty": 0})
    assert find(layer, "self_check").status is ValidationStatus.WARN
    assert "nonEmptyRate=0" in find(layer, "self_check").detail


def test_self_check_partial_hit_passes():
    conn = FakeConn([])
    layer = validate_db_ready(
        conn, "mysql", endpoint_probe=lambda: {"checked": 3, "nonEmpty": 2})
    assert find(layer, "self_check").status is ValidationStatus.PASS


def test_self_check_probe_failure_does_not_block():
    conn = FakeConn([])
    def boom():
        raise RuntimeError("endpoint down")
    layer = validate_db_ready(conn, "mysql", endpoint_probe=boom)
    assert find(layer, "self_check").status is ValidationStatus.WARN
    assert "selfCheck 不阻断" in find(layer, "self_check").detail


def test_layer_kind_is_db_ready():
    conn = FakeConn([])
    assert validate_db_ready(conn, "mysql").layer is LayerKind.DB_READY


def test_full_config_layer_passes():
    """全项 PASS 时层状态 PASS（self_check 全命中、统计信息已更新）。"""
    conn = FakeConn([
        ("information_schema.STATISTICS", [(1,)], ["orders", "i1"]),
        ("SELECT UPDATE_TIME", [("2026-11-18", None)], ["orders"]),
    ])
    layer = validate_db_ready(
        conn, "mysql", planned_indexes={"orders": ["i1"]},
        analyzed_tables=["orders"], endpoint_probe=_probe_ok())
    assert layer.status is ValidationStatus.PASS
