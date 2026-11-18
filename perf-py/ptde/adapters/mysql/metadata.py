"""MySQL 元数据采集适配器（R1 扩展采集，F2：新增而非补全）。

新适配器不改 legacy `datagen/dialects/mysql.py`（A1 分轨，legacy 保持可用）。
采集范围（实施方案 §4.4 对照表）：
  - 列（含类型精度、自增、注释、生成列 GENERATION_EXPRESSION）
  - 主键 / 声明外键（含约束名，支持复合）
  - 索引（information_schema.STATISTICS，含复合索引列序 SEQ_IN_INDEX）
  - 唯一约束（NON_UNIQUE=0 且非 PRIMARY）
  - CHECK 约束（MySQL 8.0.16+ information_schema.CHECK_CONSTRAINTS）
  - 分区（INFORMATION_SCHEMA.PARTITIONS）
  - 触发器（information_schema.TRIGGERS）
  - Sequence 登记（AUTO_INCREMENT 属性显式登记为 SequenceSchema）

A3：任一子查询失败 → MetadataCollectionError 显式失败（硬约束类不得静默降级），
错误带对象名与原因。
"""

from __future__ import annotations

import re

from ptde.models.base import Evidence, EvidenceStatus
from ptde.models.snapshot import (
    CheckConstraintSchema,
    ColumnSchema,
    DatabaseSchemaSnapshot,
    ForeignKeySchema,
    GeneratedColumnSchema,
    IndexColumn,
    IndexSchema,
    PartitionSchema,
    SequenceSchema,
    TableSchema,
    TriggerSchema,
    UniqueConstraintSchema,
)

__all__ = ["MysqlMetadataCollector", "MetadataCollectionError"]

# varchar(255) / decimal(18,2) / numeric(10, 2) 类型解析
_TYPE_RE = re.compile(
    r"^(?P<base>[A-Za-z_][A-Za-z0-9_]*)\s*(\(\s*(?P<p1>\d+)\s*(,\s*(?P<p2>\d+)\s*)?\))?",
    re.I,
)


class MetadataCollectionError(Exception):
    """方言元数据残缺/查询失败（A3 硬约束类：显式失败，带对象名与原因）。"""


def parse_type(type_name: str) -> tuple[str, int | None, int | None]:
    """解析 COLUMN_TYPE：返回 (基础类型, 长度或 precision, scale)。"""
    m = _TYPE_RE.match(type_name or "")
    if not m:
        return (type_name or "", None, None)
    base = m.group("base").lower()
    p1 = int(m.group("p1")) if m.group("p1") else None
    p2 = int(m.group("p2")) if m.group("p2") else None
    return (base, p1, p2)


class MysqlMetadataCollector:
    """MySQL DatabaseSchemaSnapshot 采集器（新适配器，不碰 legacy 方言）。"""

    product = "mysql"

    def fetch_database_schema(
        self, conn, tables: list[str] | None = None, snapshot_id: str = ""
    ) -> DatabaseSchemaSnapshot:
        """采集库级快照；tables=None 时采集当前库全部表。"""
        if tables is None:
            tables = self.list_tables(conn)
        table_schemas = [self.fetch_table_schema(conn, t) for t in tables]
        snap = DatabaseSchemaSnapshot(
            id=snapshot_id,
            database={"product": self.product, "db_version": self.db_version(conn)},
            tables=table_schemas,
        )
        snap.seal()
        return snap

    def list_tables(self, conn) -> list[str]:
        rows = self._query(
            conn,
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_NAME",
            (),
            section="list_tables",
            obj="information_schema.TABLES",
        )
        return [r[0] for r in rows]

    def db_version(self, conn) -> str | None:
        rows = self._query(conn, "SELECT VERSION()", (), section="db_version", obj="VERSION()")
        return str(rows[0][0]) if rows else None

    def fetch_table_schema(self, conn, table_name: str) -> TableSchema:
        table = TableSchema(name=table_name)
        ev_db = Evidence(
            type="database_metadata",
            object_name=f"mysql:{table_name}",
            status=EvidenceStatus.CONFIRMED,
        )
        self._collect_columns(conn, table, ev_db)
        self._collect_primary_keys(conn, table, ev_db)
        self._collect_foreign_keys(conn, table, ev_db)
        self._collect_indexes(conn, table, ev_db)
        self._collect_unique_constraints(conn, table, ev_db)
        self._collect_check_constraints(conn, table, ev_db)
        self._collect_partitions(conn, table, ev_db)
        self._collect_triggers(conn, table, ev_db)
        self._collect_sequences(conn, table, ev_db)
        self._collect_table_stats(conn, table, ev_db)
        table.evidence.append(ev_db)
        return table

    # ---------- 各子项采集 ----------

    def _collect_columns(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT COLUMN_NAME, COLUMN_TYPE, "
            "COALESCE(CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, 0), "
            "NUMERIC_SCALE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT, "
            "GENERATION_EXPRESSION "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            (table.name,),
            section="columns",
            obj=f"mysql:{table.name}.columns",
        )
        for name, ctype, _length, scale, nullable, default, extra, comment, gen_expr in rows:
            extra_l = (extra or "").lower()
            base, p1, p2 = parse_type(ctype)
            col = ColumnSchema(
                name=name,
                type_name=ctype,
                nullable=(nullable == "YES"),
                auto_increment="auto_increment" in extra_l,
                default=default,
                comment=comment,
                # 字符类型取长度，数值类型取 precision/scale
                length=p1 if base in {"char", "varchar", "binary", "varbinary", "text", "blob"} or base.endswith("text") or base.endswith("blob") else None,
                precision=p1 if base in {"decimal", "numeric"} else None,
                scale=p2 if p2 is not None else (scale if scale and int(scale) else None),
                ordinal=len(table.columns) + 1,
            )
            col.evidence.append(ev)
            table.columns.append(col)
            if gen_expr:
                kind = "STORED" if "stored" in extra_l else "VIRTUAL"
                table.generated_columns.append(
                    GeneratedColumnSchema(name=name, expression=gen_expr, kind=kind)
                )

    def _collect_primary_keys(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION",
            (table.name,),
            section="primary_keys",
            obj=f"mysql:{table.name}.primary_key",
        )
        table.primary_keys = [r[0] for r in rows if r[0] is not None]
        pk_set = {c.lower() for c in table.primary_keys}
        for col in table.columns:
            if col.name.lower() in pk_set:
                col.primary_key = True

    def _collect_foreign_keys(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT CONSTRAINT_NAME, COLUMN_NAME, ORDINAL_POSITION, "
            "REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND REFERENCED_TABLE_NAME IS NOT NULL "
            "ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION",
            (table.name,),
            section="foreign_keys",
            obj=f"mysql:{table.name}.foreign_keys",
        )
        # 按约束名分组（支持复合外键）
        grouped: dict[str, ForeignKeySchema] = {}
        for cname, fk_col, _ord, pk_table, pk_col in rows:
            fk = grouped.setdefault(
                cname,
                ForeignKeySchema(name=cname, parent_table=pk_table, evidence=[]),
            )
            fk.child_columns.append(fk_col)
            fk.parent_columns.append(pk_col)
        table.foreign_keys = list(grouped.values())

    def _collect_indexes(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, INDEX_TYPE, COLLATION "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            (table.name,),
            section="indexes",
            obj=f"mysql:{table.name}.indexes",
        )
        grouped: dict[str, IndexSchema] = {}
        for idx_name, non_unique, seq, col, idx_type, collation in rows:
            idx = grouped.setdefault(
                idx_name,
                IndexSchema(name=idx_name, unique=(not int(non_unique)), method=idx_type),
            )
            idx.columns.append(
                IndexColumn(
                    name=col,
                    ordinal=int(seq),
                    direction="DESC" if (collation or "") == "D" else None,
                )
            )
        table.indexes = list(grouped.values())

    def _collect_unique_constraints(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND NON_UNIQUE = 0 AND INDEX_NAME <> 'PRIMARY' "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            (table.name,),
            section="unique_constraints",
            obj=f"mysql:{table.name}.unique_constraints",
        )
        grouped: dict[str, UniqueConstraintSchema] = {}
        for idx_name, col, _seq in rows:
            uc = grouped.setdefault(idx_name, UniqueConstraintSchema(name=idx_name))
            uc.columns.append(col)
        table.unique_constraints = list(grouped.values())

    def _collect_check_constraints(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE "
            "FROM information_schema.TABLE_CONSTRAINTS tc "
            "JOIN information_schema.CHECK_CONSTRAINTS cc "
            "ON cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
            "AND cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA "
            "WHERE tc.TABLE_SCHEMA = DATABASE() AND tc.TABLE_NAME = %s "
            "AND tc.CONSTRAINT_TYPE = 'CHECK' ORDER BY tc.CONSTRAINT_NAME",
            (table.name,),
            section="check_constraints",
            obj=f"mysql:{table.name}.check_constraints",
        )
        for cname, clause in rows:
            table.check_constraints.append(
                CheckConstraintSchema(name=cname, expression=clause)
            )

    def _collect_partitions(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT PARTITION_NAME, PARTITION_METHOD, PARTITION_EXPRESSION, TABLE_ROWS "
            "FROM information_schema.PARTITIONS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "ORDER BY PARTITION_ORDINAL_POSITION",
            (table.name,),
            section="partitions",
            obj=f"mysql:{table.name}.partitions",
        )
        for pname, method, expression, nrows in rows:
            if pname is None:  # 非分区表返回单行 NULL 分区
                continue
            table.partitions.append(
                PartitionSchema(
                    name=pname,
                    method=method,
                    expression=expression,
                    rows=int(nrows) if nrows is not None else None,
                )
            )

    def _collect_triggers(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT TRIGGER_NAME, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT "
            "FROM information_schema.TRIGGERS "
            "WHERE EVENT_OBJECT_SCHEMA = DATABASE() AND EVENT_OBJECT_TABLE = %s "
            "ORDER BY TRIGGER_NAME",
            (table.name,),
            section="triggers",
            obj=f"mysql:{table.name}.triggers",
        )
        for tname, timing, event, statement in rows:
            table.triggers.append(
                TriggerSchema(name=tname, timing=timing, event=event, statement=statement)
            )

    def _collect_sequences(self, conn, table: TableSchema, ev: Evidence) -> None:
        """AUTO_INCREMENT 属性显式登记为 SequenceSchema（实施方案 §4.4 对照表）。"""
        auto_cols = [c for c in table.columns if c.auto_increment]
        if not auto_cols:
            return
        rows = self._query(
            conn,
            "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table.name,),
            section="sequences",
            obj=f"mysql:{table.name}.auto_increment",
        )
        current = int(rows[0][0]) if rows and rows[0][0] is not None else None
        for col in auto_cols:
            table.sequences.append(
                SequenceSchema(name=f"{table.name}.{col.name}", current_value=current, increment=1)
            )

    def _collect_table_stats(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT TABLE_ROWS, TABLE_COMMENT, ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table.name,),
            section="table_stats",
            obj=f"mysql:{table.name}.stats",
        )
        if rows:
            nrows, comment, engine = rows[0]
            table.row_estimate = int(nrows) if nrows is not None else -1
            table.table_comment = comment or None
            if engine and table.name and ev.detail is None:
                ev.detail = f"engine={engine}"

    # ---------- 查询执行（A3：失败显式抛错） ----------

    def _query(self, conn, sql: str, params: tuple, section: str, obj: str) -> list[tuple]:
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            return list(cur.fetchall())
        except Exception as exc:  # noqa: BLE001 A3 硬约束：显式失败
            raise MetadataCollectionError(
                f"{self.product} metadata '{section}' failed for {obj}: {exc}"
            ) from exc
        finally:
            cur.close()
