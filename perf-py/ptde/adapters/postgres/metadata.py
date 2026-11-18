"""PostgreSQL 元数据采集适配器（R1 扩展采集，F2：新增而非补全）。

新适配器不改 legacy `datagen/dialects/postgres.py`（A1 分轨）。采集范围：
  - 列（含默认值、SERIAL/BIGSERIAL nextval 识别）
  - 主键 / 声明外键（pg_constraint，含约束名与复合列序）
  - 索引（pg_index.indkey + unnest WITH ORDINALITY，含列序与访问方法）
  - 唯一约束（pg_constraint contype='u'）
  - CHECK 约束（pg_get_constraintdef；legacy `parse_check_definition` 语义保留复用）
  - 分区（pg_partitioned_table + pg_inherits）
  - 触发器（pg_trigger，排除 tgisinternal）
  - 生成列（pg_attribute.attgenerated）
  - Sequence（pg_sequences + serial 列默认值 nextval）

A3：任一子查询失败 → MetadataCollectionError 显式失败，错误带对象名与原因。
"""

from __future__ import annotations

import re

from ptde.adapters.mysql.metadata import (
    MetadataCollectionError,
    parse_type,
)
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

__all__ = ["PostgresMetadataCollector"]

# serial 默认值：nextval('users_id_seq'::regclass)
_NEXTVAL_RE = re.compile(r"nextval\(\s*'([^']+)'\s*(::regclass)?\s*\)", re.I)
# 分区策略代码 → 名称
_PARTSTRAT = {"r": "RANGE", "l": "LIST", "h": "HASH", "n": "RANGE"}


class PostgresMetadataCollector:
    """PostgreSQL DatabaseSchemaSnapshot 采集器（新适配器，不碰 legacy 方言）。"""

    product = "postgres"

    def fetch_database_schema(
        self, conn, tables: list[str] | None = None, snapshot_id: str = ""
    ) -> DatabaseSchemaSnapshot:
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
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND n.nspname = current_schema() ORDER BY c.relname",
            (),
            section="list_tables",
            obj="pg_class",
        )
        return [r[0] for r in rows]

    def db_version(self, conn) -> str | None:
        rows = self._query(conn, "SELECT version()", (), section="db_version", obj="version()")
        return str(rows[0][0]) if rows else None

    def fetch_table_schema(self, conn, table_name: str) -> TableSchema:
        table = TableSchema(name=table_name)
        ev = Evidence(
            type="database_metadata",
            object_name=f"postgres:{table_name}",
            status=EvidenceStatus.CONFIRMED,
        )
        self._collect_columns(conn, table, ev)
        self._collect_primary_keys(conn, table, ev)
        self._collect_foreign_keys(conn, table, ev)
        self._collect_indexes(conn, table, ev)
        self._collect_unique_constraints(conn, table, ev)
        self._collect_check_constraints(conn, table, ev)
        self._collect_partitions(conn, table, ev)
        self._collect_triggers(conn, table, ev)
        self._collect_generated_columns(conn, table, ev)
        self._collect_sequences(conn, table, ev)
        self._collect_table_stats(conn, table, ev)
        table.evidence.append(ev)
        return table

    # ---------- 各子项采集 ----------

    def _collect_columns(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT COLUMN_NAME, DATA_TYPE, "
            "COALESCE(CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, 0), "
            "NUMERIC_SCALE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (table.name,),
            section="columns",
            obj=f"postgres:{table.name}.columns",
        )
        for name, ctype, size, scale, nullable, default, comment in rows:
            base, p1, p2 = parse_type(ctype)
            col = ColumnSchema(
                name=name,
                type_name=ctype,
                nullable=(nullable == "YES"),
                auto_increment=default is not None and "nextval(" in (default or "").lower(),
                default=default,
                comment=comment,
                length=size if base in {"char", "varchar", "bpchar"} else None,
                precision=size if base in {"numeric", "decimal"} else None,
                scale=p2 if p2 is not None else (scale if scale and int(scale) else None),
                ordinal=len(table.columns) + 1,
            )
            col.evidence.append(ev)
            table.columns.append(col)

    def _collect_primary_keys(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT con.conname, ca.attname, ck.ord "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS ck(attnum, ord) ON true "
            "JOIN pg_attribute ca ON ca.attrelid = con.conrelid AND ca.attnum = ck.attnum "
            "WHERE con.contype = 'p' AND c.relname = %s "
            "ORDER BY con.conname, ck.ord",
            (table.name,),
            section="primary_keys",
            obj=f"postgres:{table.name}.primary_key",
        )
        table.primary_keys = [r[1] for r in rows]
        pk_set = {c.lower() for c in table.primary_keys}
        for col in table.columns:
            if col.name.lower() in pk_set:
                col.primary_key = True

    def _collect_foreign_keys(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT con.conname, ca.attname, pf.relname, pa.attname, ck.ord "
            "FROM pg_constraint con "
            "JOIN pg_class ch ON ch.oid = con.conrelid "
            "JOIN pg_class pf ON pf.oid = con.confrelid "
            "JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS ck(attnum, ord) ON true "
            "JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS pk(attnum, ord) ON pk.ord = ck.ord "
            "JOIN pg_attribute ca ON ca.attrelid = con.conrelid AND ca.attnum = ck.attnum "
            "JOIN pg_attribute pa ON pa.attrelid = con.confrelid AND pa.attnum = pk.attnum "
            "WHERE con.contype = 'f' AND ch.relname = %s "
            "ORDER BY con.conname, ck.ord",
            (table.name,),
            section="foreign_keys",
            obj=f"postgres:{table.name}.foreign_keys",
        )
        grouped: dict[str, ForeignKeySchema] = {}
        for cname, fk_col, pk_table, pk_col, _ord in rows:
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
            "SELECT ic.relname, ix.indisunique, am.amname, ia.attname, k.ord "
            "FROM pg_index ix "
            "JOIN pg_class ic ON ic.oid = ix.indexrelid "
            "JOIN pg_class itbl ON itbl.oid = ix.indrelid "
            "JOIN pg_am am ON am.oid = ic.relam "
            "JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON true "
            "JOIN pg_attribute ia ON ia.attrelid = ix.indrelid AND ia.attnum = k.attnum "
            "WHERE itbl.relname = %s "
            "ORDER BY ic.relname, k.ord",
            (table.name,),
            section="indexes",
            obj=f"postgres:{table.name}.indexes",
        )
        grouped: dict[str, IndexSchema] = {}
        for idx_name, unique, method, col, ord_ in rows:
            idx = grouped.setdefault(
                idx_name, IndexSchema(name=idx_name, unique=bool(unique), method=method)
            )
            idx.columns.append(IndexColumn(name=col, ordinal=int(ord_)))
        table.indexes = list(grouped.values())

    def _collect_unique_constraints(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT con.conname, ca.attname, ck.ord "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS ck(attnum, ord) ON true "
            "JOIN pg_attribute ca ON ca.attrelid = con.conrelid AND ca.attnum = ck.attnum "
            "WHERE con.contype = 'u' AND c.relname = %s "
            "ORDER BY con.conname, ck.ord",
            (table.name,),
            section="unique_constraints",
            obj=f"postgres:{table.name}.unique_constraints",
        )
        grouped: dict[str, UniqueConstraintSchema] = {}
        for cname, col, _ord in rows:
            uc = grouped.setdefault(cname, UniqueConstraintSchema(name=cname))
            uc.columns.append(col)
        table.unique_constraints = list(grouped.values())

    def _collect_check_constraints(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT con.conname, pg_get_constraintdef(con.oid) "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "WHERE con.contype = 'c' AND c.relname = %s "
            "ORDER BY con.conname",
            (table.name,),
            section="check_constraints",
            obj=f"postgres:{table.name}.check_constraints",
        )
        for cname, definition in rows:
            table.check_constraints.append(
                CheckConstraintSchema(name=cname, expression=definition)
            )

    def _collect_partitions(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT pt.partstrat, pg_get_partkeydef(p.oid) "
            "FROM pg_partitioned_table pt "
            "JOIN pg_class p ON p.oid = pt.partrelid "
            "WHERE p.relname = %s",
            (table.name,),
            section="partitions",
            obj=f"postgres:{table.name}.partitioned",
        )
        if not rows:
            return
        strat, keydef = rows[0]
        child_rows = self._query(
            conn,
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE p.relname = %s ORDER BY c.relname",
            (table.name,),
            section="partitions",
            obj=f"postgres:{table.name}.partitions",
        )
        for (child,) in child_rows:
            table.partitions.append(
                PartitionSchema(name=child, method=_PARTSTRAT.get(strat, strat), expression=keydef)
            )

    def _collect_triggers(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT t.tgname, pg_get_triggerdef(t.oid) "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = %s AND NOT t.tgisinternal ORDER BY t.tgname",
            (table.name,),
            section="triggers",
            obj=f"postgres:{table.name}.triggers",
        )
        for tname, tdef in rows:
            timing, event = self._parse_trigger_def(tdef)
            table.triggers.append(
                TriggerSchema(name=tname, timing=timing, event=event, statement=tdef)
            )

    @staticmethod
    def _parse_trigger_def(tdef: str) -> tuple[str | None, str | None]:
        timing = None
        event = None
        up = (tdef or "").upper()
        if "BEFORE" in up:
            timing = "BEFORE"
        elif "AFTER" in up:
            timing = "AFTER"
        for ev_name in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            if f" {ev_name} OR " in up or f" {ev_name} ON" in up or f" {ev_name} " in up:
                event = ev_name
                break
        return timing, event

    def _collect_generated_columns(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT a.attname, pg_get_expr(ad.adbin, ad.adrelid), a.attgenerated "
            "FROM pg_attribute a "
            "JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname = %s AND a.attgenerated <> '' "
            "ORDER BY a.attnum",
            (table.name,),
            section="generated_columns",
            obj=f"postgres:{table.name}.generated_columns",
        )
        for name, expr, attgen in rows:
            table.generated_columns.append(
                GeneratedColumnSchema(
                    name=name,
                    expression=expr,
                    kind="STORED" if attgen == "s" else "VIRTUAL",
                )
            )

    def _collect_sequences(self, conn, table: TableSchema, ev: Evidence) -> None:
        """serial 列默认值 nextval('seq') → SequenceSchema；当前值查 pg_sequences。"""
        seq_names: list[str] = []
        for col in table.columns:
            if col.auto_increment and col.default:
                m = _NEXTVAL_RE.search(col.default)
                if m:
                    seq_names.append(m.group(1))
        if not seq_names:
            return
        value_map: dict[str, tuple[int | None, int | None]] = {}
        rows = self._query(
            conn,
            "SELECT sequencename, last_value, increment_by FROM pg_sequences "
            "WHERE sequencename = ANY(%s)",
            (seq_names,),
            section="sequences",
            obj=f"postgres:{table.name}.sequences",
        )
        for sname, last, inc in rows:
            value_map[sname] = (int(last) if last is not None else None, int(inc) if inc is not None else None)
        for sname in seq_names:
            last, inc = value_map.get(sname, (None, None))
            table.sequences.append(SequenceSchema(name=sname, current_value=last, increment=inc))

    def _collect_table_stats(self, conn, table: TableSchema, ev: Evidence) -> None:
        rows = self._query(
            conn,
            "SELECT c.reltuples::bigint, obj_description(c.oid, 'pg_class') "
            "FROM pg_class c WHERE c.relname = %s",
            (table.name,),
            section="table_stats",
            obj=f"postgres:{table.name}.stats",
        )
        if rows:
            reltuples, comment = rows[0]
            table.row_estimate = int(reltuples) if reltuples is not None and reltuples >= 0 else -1
            table.table_comment = comment or None

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
