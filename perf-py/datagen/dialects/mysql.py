"""MySQL 方言：移植自 dialect/mysql/MySQLDialect.java。

承接原 DataGenService 的 MySQL 特有逻辑：URL 增强 rewriteBatchedStatements、
information_schema 元数据读取（列注释/自增/主键/外键）、TYPE_NAME 内联 ENUM
解析、反引号标识符。

Java→Python 适配：JDBC DatabaseMetaData 换成 information_schema 直查；
参数占位符由类属性 param 提供（pymysql=%s，测试方言可覆盖为 ?）。
"""

from __future__ import annotations

import re

from datagen.dialects import DRIVER_HINT, parse_jdbc_url
from datagen.model import ColumnMeta, FkMeta, SchemaMetadata
from datagen.spi import DatabaseDialect

# ENUM('A','B') / SET('X') 类型名解析
QUOTED = re.compile(r"'([^']*)'")


class MysqlDialect(DatabaseDialect):
    """MySQL 方言。"""

    # DBAPI 参数占位符（pymysql/MySQLdb 为 %s；测试方言可覆盖）
    param = "%s"

    def supports(self, jdbc_url: str | None) -> bool:
        return jdbc_url is not None and jdbc_url.lower().startswith("jdbc:mysql://")

    def enrich_jdbc_url(self, jdbc_url: str) -> str:
        if "rewriteBatchedStatements" in jdbc_url:
            return jdbc_url
        return jdbc_url + ("&" if "?" in jdbc_url else "?") \
            + "rewriteBatchedStatements=true&useInformationSchema=true"

    def connect(self, jdbc_url: str, user: str, password: str):
        """生产连接：pymysql（懒加载；缺失时报可执行的安装指引）。"""
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - 环境差异路径
            raise RuntimeError(f"MySQL {DRIVER_HINT}") from exc
        host, port, database, _params = parse_jdbc_url(jdbc_url, "mysql")
        return pymysql.connect(
            host=host,
            port=port or 3306,
            user=user,
            password=password or "",
            database=database,
            charset="utf8mb4",
            autocommit=False,
        )

    def fetch_metadata(self, conn, table_name: str) -> SchemaMetadata:
        meta = SchemaMetadata(table_name=table_name)
        p = self.param
        # 列（JDBC getColumns 等价：COLUMN_TYPE 承接 TYPE_NAME，长度取
        # 字符/数值精度其一，IS_NULLABLE/自增/注释同语义）
        sql = (
            "SELECT COLUMN_NAME, COLUMN_TYPE, "
            "COALESCE(CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, 0), "
            "IS_NULLABLE, EXTRA, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = {p} ORDER BY ORDINAL_POSITION"
        )
        cur = conn.cursor()
        try:
            cur.execute(sql, (table_name,))
            for name, ctype, size, nullable, extra, comment in cur.fetchall():
                meta.columns.append(ColumnMeta(
                    name=name,
                    type_name=ctype,
                    size=int(size or 0),
                    nullable=(nullable == "YES"),
                    auto_increment=bool(extra and "auto_increment" in extra.lower()),
                    comment=comment,
                ))
        finally:
            cur.close()
        # 主键
        sql = ("SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
               "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = " + p
               + " AND CONSTRAINT_NAME = 'PRIMARY'")
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
        sql = ("SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
               "FROM information_schema.KEY_COLUMN_USAGE "
               "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = " + p
               + " AND REFERENCED_TABLE_NAME IS NOT NULL")
        cur = conn.cursor()
        try:
            cur.execute(sql, (table_name,))
            for fk_col, pk_table, pk_col in cur.fetchall():
                meta.foreign_keys.append(FkMeta(fk_col, pk_table, pk_col))
        finally:
            cur.close()
        # 行数（快速 COUNT；失败置 -1 不阻断）
        meta.row_count = self.table_row_count(conn, table_name)
        return meta

    def table_row_count(self, conn, table_name: str) -> int:
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT COUNT(*) FROM `" + table_name.replace("`", "") + "`")
                row = cur.fetchone()
                return int(row[0]) if row else -1
            finally:
                cur.close()
        except Exception:  # noqa: BLE001 失败置 -1 不阻断
            return -1

    def get_enum_values(self, conn, table_name: str | None, column_name: str | None) -> list[str]:
        """MySQL 列内 ENUM 由 COLUMN_TYPE 解析（供独立调用场景重查）。"""
        vals: list[str] = []
        p = self.param
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = " + p
                    + " AND COLUMN_NAME = " + p,
                    (table_name, column_name),
                )
                row = cur.fetchone()
            finally:
                cur.close()
            if row and row[0] and row[0].upper().startswith("ENUM"):
                vals.extend(QUOTED.findall(row[0]))
        except Exception:  # noqa: BLE001 降级：返回空列表，由后续枚举来源接管
            pass
        return vals

    def quote_identifier(self, name: str | None) -> str:
        return "`" + ("" if name is None else name.replace("`", "``")) + "`"

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
            sql += self.param  # 值一律占位符（JDBC ? 等价；pymysql=%s）
        sql += ")"
        return sql
