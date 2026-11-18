"""T5.2 Capability Probe 测试（实施方案 §8.2；总方案 §10.1/§10.2）。

覆盖：§10.1 探测项全集填充、探测失败降为未知（None + notes，不伪装）、
快照形态（§10.2 键集，探测填充非复制示例）、ExecutionManifest 回填。
"""

from __future__ import annotations

import pytest

from ptde.adapters.probe import (
    CAPABILITY_KEYS,
    AdapterCapability,
    CapabilityProbeError,
    MysqlCapabilityProbe,
    PostgresCapabilityProbe,
    probe_adapter,
)
from ptde.models.spec import ExecutionManifest


class FakeCursor:
    """按 SQL 关键字路由返回行的假游标（鸭子类型，不依赖驱动）。"""

    def __init__(self, routes: dict[str, list[tuple]]) -> None:
        self._routes = routes

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
        self._result: list[tuple] = []
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


MYSQL_ROUTES = {
    "SELECT VERSION()": [("8.0.36",)],
    "version_comment": [("MySQL Community Server - GPL",)],
    "default_storage_engine": [("InnoDB",)],
    "SELECT @@sql_mode": [("STRICT_TRANS_TABLES,STRICT_ALL_TABLES",)],
    "SELECT @@local_infile": [("1",)],
    "SELECT @@secure_file_priv": [("/var/lib/mysql-files/",)],
    "SELECT @@max_allowed_packet": [(67108864,)],
    # §10.1 读取类能力存在性查询（COUNT 恒返一行）
    "FROM information_schema.TABLES WHERE TABLE_SCHEMA": [(5,)],
    "information_schema.STATISTICS": [(8,)],
    "KEY_COLUMN_USAGE": [(2,)],
    "information_schema.PARTITIONS": [(3,)],
    "information_schema.TRIGGERS": [(1,)],
    "EXTRA LIKE": [(1,)],
    "DATA_TYPE IN": [(4,)],
}

PG_ROUTES = {
    "SELECT version()": [("PostgreSQL 16.3 (Debian) on x86_64",)],
    "server_version_num": [("160003",)],
    "pg_catalog.pg_attribute": [(1,)],  # catalog 列存在性（deferred/RLS/索引/约束/分区）
    "pg_stats": [(3,)],
    "information_schema.sequences": [(2,)],
    "pg_catalog.pg_type": [(4,)],
    "pg_catalog.pg_trigger": [(1,)],
    "has_schema_privilege": [(True,)],
}


# ---------- §10.1 探测项全集 ----------


def test_mysql_probe_fills_all_section_101_items() -> None:
    cap = MysqlCapabilityProbe().probe(FakeConn(MYSQL_ROUTES))
    assert set(cap.capabilities) == set(CAPABILITY_KEYS)  # 探测项全集
    assert cap.product == "mysql" and cap.db_version == "8.0.36"
    assert cap.engine == "InnoDB"
    assert cap.compat_mode == "native"  # STRICT 模式无 ANSI 线索 → native
    assert cap.capabilities["schema_inspection"] is True
    assert cap.capabilities["index_inspection"] is True
    assert cap.capabilities["foreign_key_inspection"] is True
    assert cap.capabilities["partition_inspection"] is True
    assert cap.capabilities["statistics_read"] is True
    assert cap.capabilities["sequence_support"] is True
    assert cap.capabilities["deferred_constraints"] is False  # MySQL 无 DEFERRABLE
    assert cap.capabilities["trigger_fires_on_bulk_load"] is True
    assert cap.capabilities["parallel_load"] is False
    assert cap.capabilities["post_load_analyze"] is True  # 推定项（notes 说明）
    # bulk_load 子对象：探测填充（QW4 口径）
    assert cap.bulk_load["selected_strategy"] == "load_data_local"
    assert cap.bulk_load["requires_client_local_infile"] is True
    assert cap.bulk_load["server_local_infile"] is True
    assert cap.bulk_load["secure_file_priv"] == "/var/lib/mysql-files/"
    assert cap.bulk_load["secure_file_priv_scope"] == "server_infile_only"
    assert cap.capabilities["bulk_load"] is True


def test_mysql_probe_mariadb_variant_and_ansi_mode() -> None:
    routes = dict(MYSQL_ROUTES)
    routes["version_comment"] = [("MariaDB 10.6",)]
    routes["SELECT @@sql_mode"] = [("ANSI_QUOTES,ERROR_FOR_DIVISION_BY_ZERO",)]
    cap = MysqlCapabilityProbe().probe(FakeConn(routes))
    assert cap.compat_mode == "mariadb"  # 变体探测优先于 sql_mode 线索


def test_mysql_probe_version_unavailable_fails_explicitly() -> None:
    # 无法识别环境禁止写入（总方案 §13.1）：版本不可得显式失败
    with pytest.raises(CapabilityProbeError, match="无法获取版本"):
        MysqlCapabilityProbe().probe(FakeConn({}))


def test_pg_probe_fills_all_section_101_items() -> None:
    cap = PostgresCapabilityProbe().probe(FakeConn(PG_ROUTES))
    assert set(cap.capabilities) == set(CAPABILITY_KEYS)
    assert cap.product == "postgres" and cap.db_version.startswith("PostgreSQL 16")
    assert cap.compat_mode == "native"
    assert cap.capabilities["deferred_constraints"] is True  # pg_constraint.deferrable 实测
    assert cap.capabilities["sequence_support"] is True
    assert cap.capabilities["bulk_load"] is None  # 未设路由 → 未知（不伪装）
    assert cap.bulk_load["selected_strategy"] == "copy_from_stdin"
    # pg_class.relrowsecurity 列存在性实测（逐表开关归 T5.4/T5.6 precheck 判定）
    assert cap.bulk_load["rls_feature_present"] is True
    assert cap.bulk_load["current_schema_create"] is True


def test_probe_single_item_failure_degrades_to_unknown_with_note() -> None:
    class PartialConn:
        """local_infile 查询抛错的假连接（其余项正常）。"""

        def cursor(self):
            return _PartialCursor()

    class _PartialCursor:
        def execute(self, sql, params=()):
            if "@@local_infile" in sql:
                raise RuntimeError("access denied for SHOW VARIABLES")
            self._r = {
                "SELECT VERSION()": [("8.0.36",)],
                "version_comment": [("MySQL",)],
                "default_storage_engine": [("InnoDB",)],
                "SELECT @@sql_mode": [("STRICT_TRANS_TABLES",)],
            }.get(sql, None)
            if self._r is None:
                # 其余查询统一返回存在性 1（读取类能力可用）
                self._r = [(1,)]

        def fetchall(self):
            return self._r

        def fetchone(self):
            return self._r[0] if self._r else None

        def close(self):
            pass

    cap = MysqlCapabilityProbe().probe(PartialConn())
    assert cap.capabilities["bulk_load"] is None  # 探测失败 → 未知，不伪装可用
    assert any("local_infile" in n for n in cap.notes)
    assert any("不伪装可用" in n for n in cap.notes)


# ---------- §10.2 快照形态与 ExecutionManifest 回填（门禁 6） ----------


def test_capability_snapshot_rejects_missing_keys() -> None:
    # 探测项全集契约：缺项显式失败（不得缺省伪装成已探测）
    with pytest.raises(ValueError, match="缺少 §10.1 探测项"):
        AdapterCapability(product="mysql", capabilities={"schema_inspection": True})


def test_capability_snapshot_seals_and_json_native() -> None:
    cap = MysqlCapabilityProbe().probe(FakeConn(MYSQL_ROUTES))
    snap = cap.to_snapshot()
    assert snap["capabilities"]["schema_inspection"] is True
    assert cap.fingerprint  # 快照带指纹（随 manifest 入库可审计）
    # JSON 原生（协议 §7.5）：直接可入 ExecutionManifest.adapter_capability_snapshot
    manifest = ExecutionManifest(id="m1")
    manifest.adapter_capability_snapshot = snap
    assert manifest.adapter_capability_snapshot["product"] == "mysql"


def test_capability_fingerprint_excludes_probed_at() -> None:
    from datetime import UTC, datetime

    cap1 = MysqlCapabilityProbe().probe(FakeConn(MYSQL_ROUTES))
    cap2 = MysqlCapabilityProbe().probe(FakeConn(MYSQL_ROUTES))
    cap1.probed_at = datetime(2026, 1, 1, tzinfo=UTC)
    cap2.probed_at = datetime(2026, 6, 1, tzinfo=UTC)
    assert cap1.seal().fingerprint == cap2.seal().fingerprint  # 时间戳不入指纹


def test_probe_adapter_dispatch_and_unknown_product() -> None:
    cap = probe_adapter(FakeConn(MYSQL_ROUTES), "mysql")
    assert cap.product == "mysql"
    cap = probe_adapter(FakeConn(PG_ROUTES), "PostgreSQL")  # 大小写归一
    assert cap.product == "postgres"
    with pytest.raises(CapabilityProbeError, match="无 Capability 探测器"):
        probe_adapter(FakeConn({}), "oceanbase")
