"""PostgreSQL 方言：移植自 dialect/postgres/PostgreSQLDialect.java，与 MySQLDialect 逐方法对齐。

差异点：URL jdbc:postgresql:（增强 reWriteBatchedInserts + stringtype=unspecified）；
元数据走 information_schema + pg_catalog；枚举发现 pg_enum / CHECK 约束文本解析；
SERIAL/BIGSERIAL 默认值 nextval 识别为自增；双引号标识符。

容错：所有 information_schema / pg_catalog 查询失败均静默降级，绝不因元数据缺失中断灌数。
"""

from __future__ import annotations

import re

from ptde.adapters.direct.dialects import DRIVER_HINT, parse_jdbc_url
from ptde.adapters.direct.spi import DatabaseDialect
from ptde.models.wire import ColumnMeta, FkMeta, SchemaMetadata

# PG 约束定义中的字符串字面量
QUOTED = re.compile(r"'([^']*)'")
# 类型强制转换（::text / ::character varying(32) / ::numeric(10,2)）
CAST = re.compile(r"::\s*[A-Za-z_][A-Za-z0-9_\s]*(\(\s*\d+\s*(,\s*\d+\s*)?\))?")
# 数值枚举：CHECK (col IN (1, 2, 3))
IN_LIST = re.compile(r"\bin\s*\(([^)]+)\)", re.I)


class PostgresDialect(DatabaseDialect):
    """PostgreSQL 方言。"""

    param = "%s"

    def supports(self, jdbc_url: str | None) -> bool:
        return jdbc_url is not None and jdbc_url.lower().startswith("jdbc:postgresql:")

    def enrich_jdbc_url(self, jdbc_url: str) -> str:
        url = jdbc_url
        # reWriteBatchedInserts：多行 VALUES 合并（等价 MySQL rewriteBatchedStatements）
        if "reWriteBatchedInserts" not in url:
            url += ("&" if "?" in url else "?") + "reWriteBatchedInserts=true"
        # stringtype=unspecified：参数按目标列类型推断（原生枚举/JSON 列写入必需）
        if "stringtype" not in url:
            url += ("&" if "?" in url else "?") + "stringtype=unspecified"
        return url

    def connect(self, jdbc_url: str, user: str, password: str):
        """生产连接：优先 psycopg2，回退 psycopg3（均懒加载）。"""
        host, port, database, _params = parse_jdbc_url(jdbc_url, "postgresql")
        kwargs = {
            "host": host,
            "port": port or 5432,
            "user": user,
            "password": password or "",
            "dbname": database,
        }
        try:
            import psycopg2
        except ImportError:
            pass
        else:
            return psycopg2.connect(**kwargs)
        try:
            import psycopg  # noqa: PLC0415  psycopg3 回退
        except ImportError as exc:  # pragma: no cover - 环境差异路径
            raise RuntimeError(f"PostgreSQL {DRIVER_HINT}") from exc
        return psycopg.connect(**kwargs)

    def fetch_metadata(self, conn, table_name: str) -> SchemaMetadata:
        meta = SchemaMetadata(table_name=table_name)
        p = self.param
        sql = (
            "SELECT COLUMN_NAME, DATA_TYPE, "
            "COALESCE(CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, 0), "
            "IS_NULLABLE, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS WHERE TABLE_NAME = " + p
            + " ORDER BY ORDINAL_POSITION"
        )
        cur = conn.cursor()
        try:
            cur.execute(sql, (table_name,))
            for name, ctype, size, nullable, comment in cur.fetchall():
                meta.columns.append(ColumnMeta(
                    name=name,
                    type_name=ctype,
                    size=int(size or 0),
                    nullable=(nullable == "YES"),
                    comment=comment,
                ))
        finally:
            cur.close()
        # 主键
        sql = ("SELECT kcu.COLUMN_NAME FROM information_schema.TABLE_CONSTRAINTS tc "
               "JOIN information_schema.KEY_COLUMN_USAGE kcu "
               "ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
               "AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA "
               "WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_NAME = " + p)
        cur = conn.cursor()
        try:
            cur.execute(sql, (table_name,))
            for (pk,) in cur.fetchall():
                if pk is None:
                    continue
                meta.primary_keys.add(pk.lower())
                c = meta.find_column(pk)
                if c is not None:
                    c.primary_key = True
        finally:
            cur.close()
        # 声明外键（ImportedKeys：本表引用的父表）
        sql = ("SELECT kcu.COLUMN_NAME, ccu.TABLE_NAME, ccu.COLUMN_NAME "
               "FROM information_schema.TABLE_CONSTRAINTS tc "
               "JOIN information_schema.KEY_COLUMN_USAGE kcu "
               "ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
               "AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA "
               "JOIN information_schema.CONSTRAINT_COLUMN_USAGE ccu "
               "ON ccu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
               "AND ccu.TABLE_SCHEMA = tc.TABLE_SCHEMA "
               "WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY' AND tc.TABLE_NAME = " + p)
        cur = conn.cursor()
        try:
            cur.execute(sql, (table_name,))
            for fk_col, pk_table, pk_col in cur.fetchall():
                meta.foreign_keys.append(FkMeta(fk_col, pk_table, pk_col))
        finally:
            cur.close()
        # SERIAL/BIGSERIAL：默认值 nextval(...) 识别为自增（驱动元数据覆盖不全）
        self._mark_serial_defaults(conn, meta, table_name)
        meta.row_count = self.table_row_count(conn, table_name)
        return meta

    def table_row_count(self, conn, table_name: str) -> int:
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT COUNT(*) FROM " + self.quote_identifier(table_name))
                row = cur.fetchone()
                return int(row[0]) if row else -1
            finally:
                cur.close()
        except Exception:  # noqa: BLE001 失败置 -1 不阻断
            return -1

    def get_enum_values(self, conn, table_name: str | None, column_name: str | None) -> list[str]:
        vals: list[str] = []
        if conn is None or not column_name or not column_name.strip():
            return vals
        # 1) 原生枚举类型：information_schema.udt_name → pg_type → pg_enum
        vals.extend(self._native_enum_values(conn, table_name, column_name))
        if vals:
            return vals
        # 2) CHECK 约束（PG 无列内 ENUM，CHECK 是最常见替代方案）
        return self._check_enum_values(conn, table_name, column_name)

    def quote_identifier(self, name: str | None) -> str:
        return "\"" + ("" if name is None else name.replace("\"", "\"\"")) + "\""

    def build_insert_sql(self, table_name: str, columns: list[str]) -> str:
        sql = "INSERT INTO " + self.quote_identifier(table_name) + " ("
        for i, col in enumerate(columns):
            if i > 0:
                sql += ", "
            sql += self.quote_identifier(col)
        sql += ") VALUES ("
        for i in range(len(columns)):
            if i > 0:
                sql += ", "
            sql += self.param  # 值一律占位符（JDBC ? 等价；psycopg2=%s）
        sql += ")"
        return sql

    def _mark_serial_defaults(self, conn, meta: SchemaMetadata, table_name: str) -> None:
        sql = ("SELECT COLUMN_NAME, COLUMN_DEFAULT FROM information_schema.COLUMNS "
               "WHERE TABLE_NAME = " + self.param)
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, (table_name,))
                for name, default in cur.fetchall():
                    col = meta.find_column(name)
                    if col is not None and is_serial_default(default):
                        col.auto_increment = True
            finally:
                cur.close()
        except Exception:  # noqa: BLE001 无 information_schema 访问权时降级
            pass

    def _native_enum_values(self, conn, table_name: str | None, column_name: str) -> list[str]:
        vals: list[str] = []
        if not table_name or not table_name.strip():
            return vals
        p = self.param
        sql = ("SELECT e.enumlabel FROM information_schema.columns c "
               "JOIN pg_type t ON t.typname = c.udt_name "
               "JOIN pg_enum e ON e.enumtypid = t.oid "
               "WHERE c.table_name = " + p + " AND c.column_name = " + p
               + " ORDER BY e.enumsortorder")
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, (table_name.lower(), column_name.lower()))
                for (label,) in cur.fetchall():
                    vals.append(label)
            finally:
                cur.close()
        except Exception:  # noqa: BLE001 无 pg_enum（兼容模式）降级 CHECK 发现
            pass
        return vals

    def _check_enum_values(self, conn, table_name: str | None, column_name: str) -> list[str]:
        vals: list[str] = []
        if not table_name or not table_name.strip():
            return vals
        p = self.param
        sql = ("SELECT pg_get_constraintdef(con.oid) FROM pg_constraint con "
               "JOIN pg_class rel ON rel.oid = con.conrelid "
               "JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = ANY(con.conkey) "
               "WHERE con.contype = 'c' AND rel.relname = " + p + " AND att.attname = " + p)
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, (table_name.lower(), column_name.lower()))
                for (definition,) in cur.fetchall():
                    for v in parse_check_definition(definition):
                        if v not in vals:
                            vals.append(v)
            finally:
                cur.close()
        except Exception:  # noqa: BLE001 无 pg_catalog 视图时降级
            pass
        return vals


def is_serial_default(column_default: str | None) -> bool:
    """SERIAL/BIGSERIAL 判定：列默认值为 nextval('..._id_seq'::regclass)。"""
    return column_default is not None and "nextval(" in column_default.lower()


def parse_check_definition(definition: str | None) -> list[str]:
    """解析 PG 约束定义中的候选值。

    CHECK ((status = ANY (ARRAY['PENDING'::character varying, 'PAID'::character varying])))
    CHECK ((status IN ('A', 'B'))) / CHECK ((level IN (1, 2, 3)))
    """
    vals: list[str] = []
    if definition is None or not definition.strip():
        return vals
    s = CAST.sub("", definition)
    vals.extend(v for v in QUOTED.findall(s) if v not in vals)
    if vals:
        return vals
    m = IN_LIST.search(s)
    if m:
        for part in m.group(1).split(","):
            v = part.strip()
            if v and v not in vals:
                vals.append(v)
    return vals
