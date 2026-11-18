"""T1.6 DatabaseSchemaSnapshot 扩展采集测试（fake DBAPI 连接，不连真库）。

覆盖：MySQL/PG 新适配器对索引列序、分区、触发器、生成列、Sequence、
Check 约束的采集；A3 硬约束显式失败路径；确认不触碰 legacy 方言。
"""

from __future__ import annotations

import pytest

from ptde.adapters.mysql import MysqlMetadataCollector
from ptde.adapters.mysql.metadata import MetadataCollectionError, parse_type
from ptde.adapters.postgres import PostgresMetadataCollector


class FakeCursor:
    """按 SQL 关键字路由返回行的假游标（鸭子类型，不依赖驱动）。"""

    def __init__(self, routes: list[tuple[str, list[tuple]]]) -> None:
        self._routes = routes

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
        self._result: list[tuple] = []
        for marker, rows in self._routes:
            if marker in sql:
                self._result = rows
                return
        self._result = []

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def close(self) -> None:
        pass


class FakeConn:
    def __init__(self, routes: list[tuple[str, list[tuple]]]) -> None:
        self._routes = routes

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._routes)


# ---------- MySQL ----------

MYSQL_ROUTES = [
    ("information_schema.COLUMNS WHERE TABLE_SCHEMA", [
        ("id", "bigint unsigned", 20, 0, "NO", None, "auto_increment", "", ""),
        ("status", "varchar(20)", 20, None, "YES", "'NEW'", "", "订单状态", ""),
        ("amount", "decimal(18,2)", 18, 2, "NO", None, "", "", ""),
        ("full_name", "varchar(120)", 120, None, "YES", None, "VIRTUAL GENERATED", "", "CONCAT(first, ' ', last)"),
    ]),
    ("CONSTRAINT_NAME = 'PRIMARY'", [("id",)]),
    ("REFERENCED_TABLE_NAME IS NOT NULL", [
        ("fk_orders_user", "user_id", 1, "users", "id"),
    ]),
    ("INDEX_TYPE, COLLATION", [
        ("PRIMARY", 0, 1, "id", "BTREE", "A"),
        ("idx_status_created", 1, 1, "status", "BTREE", "A"),
        ("idx_status_created", 1, 2, "created_at", "BTREE", "D"),
        ("uk_order_no", 0, 1, "order_no", "BTREE", "A"),
    ]),
    ("AND INDEX_NAME <> 'PRIMARY'", [("uk_order_no", "order_no", 1)]),
    ("CONSTRAINT_TYPE = 'CHECK'", [("chk_status", "((`status` in ('NEW','PAID')))")]),
    ("information_schema.PARTITIONS", [
        ("p2026", "RANGE", "to_days(created_at)", 123),
        (None, "RANGE", "to_days(created_at)", 0),
    ]),
    ("information_schema.TRIGGERS", [
        ("trg_order_audit", "AFTER", "INSERT", "BEGIN INSERT INTO audit ... END"),
    ]),
    ("SELECT AUTO_INCREMENT", [(10001,)]),
    ("TABLE_ROWS, TABLE_COMMENT, ENGINE", [(1234, "订单表", "InnoDB")]),
    ("TABLE_TYPE = 'BASE TABLE'", [("orders",), ("users",)]),
    ("SELECT VERSION()", [("8.0.36",)]),
]


def test_mysql_full_table_schema() -> None:
    snap = MysqlMetadataCollector().fetch_database_schema(
        FakeConn(MYSQL_ROUTES), tables=["orders"], snapshot_id="snap-1"
    )
    assert snap.id == "snap-1"
    assert snap.database.product == "mysql"
    assert snap.database.db_version == "8.0.36"
    assert snap.fingerprint  # 快照带指纹
    t = snap.tables[0]
    assert t.name == "orders"
    # 列 + 生成列
    assert [c.name for c in t.columns] == ["id", "status", "amount", "full_name"]
    status = t.columns[1]
    assert status.type_name == "varchar(20)"
    assert status.length == 20
    assert status.nullable is True
    assert status.comment == "订单状态"
    assert t.columns[0].primary_key is True and t.primary_keys == ["id"]
    amount = t.columns[2]
    assert amount.precision == 18 and amount.scale == 2
    # 生成列（VIRTUAL GENERATED）
    assert len(t.generated_columns) == 1
    gen = t.generated_columns[0]
    assert gen.name == "full_name" and gen.kind == "VIRTUAL"
    assert gen.expression == "CONCAT(first, ' ', last)"
    # 复合索引列序（SEQ_IN_INDEX）
    idx = {i.name: i for i in t.indexes}
    assert idx["idx_status_created"].columns[0].ordinal == 1
    assert idx["idx_status_created"].columns[1].name == "created_at"
    assert idx["idx_status_created"].columns[1].ordinal == 2
    assert idx["idx_status_created"].columns[1].direction == "DESC"
    assert idx["uk_order_no"].unique is True
    # 唯一约束（PRIMARY 之外）
    assert [u.name for u in t.unique_constraints] == ["uk_order_no"]
    # CHECK 约束（MySQL 8）
    assert t.check_constraints[0].name == "chk_status"
    # 分区（NULL 分区行跳过）
    assert [p.name for p in t.partitions] == ["p2026"]
    assert t.partitions[0].method == "RANGE"
    # 触发器
    assert t.triggers[0].name == "trg_order_audit" and t.triggers[0].timing == "AFTER"
    # AUTO_INCREMENT 显式登记为 Sequence
    assert t.sequences[0].name == "orders.id"
    assert t.sequences[0].current_value == 10001
    # 行数近似 + 表注释
    assert t.row_estimate == 1234
    assert t.table_comment == "订单表"


def test_mysql_metadata_failure_is_explicit_hard_failure() -> None:
    class BrokenConn:
        class _Cur:
            def execute(self, sql, params=()):
                raise RuntimeError("simulated permission denied")

            def fetchall(self):
                return []

            def close(self):
                pass

        def cursor(self):
            return BrokenConn._Cur()

    # A3：元数据残缺属硬约束类 → 显式失败而非静默降级
    with pytest.raises(MetadataCollectionError, match="columns"):
        MysqlMetadataCollector().fetch_table_schema(BrokenConn(), "orders")


def test_mysql_empty_partitions_and_no_auto_increment() -> None:
    routes = [
        ("information_schema.COLUMNS WHERE TABLE_SCHEMA", [
            ("id", "bigint", 20, 0, "NO", None, "", "", ""),
        ]),
        ("CONSTRAINT_NAME = 'PRIMARY'", [("id",)]),
        ("REFERENCED_TABLE_NAME IS NOT NULL", []),
        ("INDEX_TYPE, COLLATION", [("PRIMARY", 0, 1, "id", "BTREE", "A")]),
        ("AND INDEX_NAME <> 'PRIMARY'", []),
        ("CONSTRAINT_TYPE = 'CHECK'", []),
        ("information_schema.PARTITIONS", [(None, None, None, None)]),
        ("information_schema.TRIGGERS", []),
        ("TABLE_ROWS, TABLE_COMMENT, ENGINE", [(10, None, "InnoDB")]),
    ]
    t = MysqlMetadataCollector().fetch_table_schema(FakeConn(routes), "simple")
    assert t.partitions == [] and t.sequences == [] and t.triggers == []
    assert t.row_estimate == 10 and t.table_comment is None


def test_parse_type_variants() -> None:
    assert parse_type("varchar(255)") == ("varchar", 255, None)
    assert parse_type("decimal(18,2)") == ("decimal", 18, 2)
    assert parse_type("bigint") == ("bigint", None, None)


# ---------- PostgreSQL ----------

PG_ROUTES = [
    ("information_schema.COLUMNS WHERE TABLE_NAME", [
        ("id", "bigint", 20, 0, "NO", "nextval('orders_id_seq'::regclass)", None),
        ("status", "character varying", 20, None, "YES", "'NEW'", None),
        ("amount", "numeric", 18, 2, "NO", None, None),
    ]),
    ("con.contype = 'p'", [("orders_pkey", "id", 1)]),
    ("con.contype = 'f'", [("fk_orders_user", "user_id", "users", "id", 1)]),
    ("pg_index ix", [
        ("orders_pkey", True, "btree", "id", 1),
        ("idx_status", False, "btree", "status", 1),
    ]),
    ("con.contype = 'u'", [("uk_order_no", "order_no", 1)]),
    ("con.contype = 'c'", [("chk_status", "CHECK ((status = ANY (ARRAY['NEW'::text, 'PAID'::text])))")]),
    ("pg_partitioned_table", [("r", "RANGE (created_at)")]),
    ("pg_inherits i", [("orders_p2025",), ("orders_p2026",)]),
    ("pg_trigger t", [
        ("trg_audit", "CREATE TRIGGER trg_audit AFTER INSERT ON orders FOR EACH ROW EXECUTE FUNCTION audit()"),
    ]),
    ("pg_attrdef", [("full_name", "(first || last)", "s")]),
    ("pg_sequences", [("orders_id_seq", 9999, 1)]),
    ("reltuples", [(5555, "订单表")]),
    ("relkind = 'r'", [("orders",), ("users",)]),
    ("SELECT version()", [("PostgreSQL 16.2",)]),
]


def test_postgres_full_table_schema() -> None:
    snap = PostgresMetadataCollector().fetch_database_schema(
        FakeConn(PG_ROUTES), tables=["orders"], snapshot_id="snap-pg"
    )
    assert snap.database.product == "postgres"
    assert snap.fingerprint
    t = snap.tables[0]
    assert t.name == "orders"
    # 列 + serial 自增识别
    assert t.columns[0].auto_increment is True
    assert t.columns[1].type_name == "character varying"
    assert t.columns[2].precision == 18 and t.columns[2].scale == 2
    assert t.primary_keys == ["id"] and t.columns[0].primary_key is True
    # 声明外键（含约束名）
    assert t.foreign_keys[0].name == "fk_orders_user"
    assert t.foreign_keys[0].parent_table == "users"
    # 索引含访问方法
    idx = {i.name: i for i in t.indexes}
    assert idx["idx_status"].method == "btree"
    assert idx["orders_pkey"].unique is True
    # 唯一约束
    assert [u.name for u in t.unique_constraints] == ["uk_order_no"]
    # CHECK 约束（PG 保留解析路径）
    assert t.check_constraints[0].expression.startswith("CHECK")
    # 分区（父表策略 + 子分区清单）
    assert [p.name for p in t.partitions] == ["orders_p2025", "orders_p2026"]
    assert t.partitions[0].method == "RANGE"
    # 触发器（排除内部触发器由 SQL 条件承接）
    assert t.triggers[0].name == "trg_audit" and t.triggers[0].timing == "AFTER"
    # 生成列（attgenerated='s' → STORED）
    assert t.generated_columns[0].kind == "STORED"
    # Sequence（pg_sequences 当前值）
    assert t.sequences[0].name == "orders_id_seq"
    assert t.sequences[0].current_value == 9999
    # reltuples 行数估算
    assert t.row_estimate == 5555
    assert t.table_comment == "订单表"


def test_postgres_metadata_failure_is_explicit_hard_failure() -> None:
    class BrokenConn:
        class _Cur:
            def execute(self, sql, params=()):
                raise RuntimeError("simulated pg_catalog denied")

            def fetchall(self):
                return []

            def close(self):
                pass

        def cursor(self):
            return BrokenConn._Cur()

    with pytest.raises(MetadataCollectionError, match="columns"):
        PostgresMetadataCollector().fetch_table_schema(BrokenConn(), "orders")


def test_legacy_dialects_untouched() -> None:
    """T1.6 约束：新适配器不修改 legacy 方言（源码扫描断言）。"""
    import inspect

    from datagen.dialects.mysql import MysqlDialect
    from datagen.dialects.postgres import PostgresDialect

    assert "information_schema.STATISTICS" not in inspect.getsource(MysqlDialect)
    assert "pg_partitioned_table" not in inspect.getsource(PostgresDialect)
