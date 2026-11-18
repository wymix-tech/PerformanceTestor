"""MySQL 统计采集子适配器（R2 T2.2，新建不改 legacy 方言，A1 分轨）。

采集项对齐实施方案 §5.4 表：
  - 表行数：information_schema.TABLES.TABLE_ROWS（approximate）
  - 字段基数/NULL 比例/Top-K/长度：采样子查询（estimated）
  - 敏感列 Top-K：SQL 侧 MD5 哈希分桶（明文不出库）
  - 关联基数：子表按父键 GROUP BY（父键哈希分桶）

约定：适配器只提供 SQL 原语，全部经采集器下发的只读查询回调 `q(sql, params, obj)`
执行（发送前语句白名单校验，白名单外直接拒绝；失败带对象名与原因显式上抛）。
标识符（表/列名）拼接前必须过 validate_identifier，防注入。
"""

from __future__ import annotations

import re

__all__ = [
    "MysqlStatisticsAdapter",
    "StatisticsCollectionError",
    "validate_identifier",
]

# 标识符白名单：防注入（表/列名不允许出引号、空白、点以外的奇怪字符）
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


class StatisticsCollectionError(Exception):
    """统计采集失败（A3 硬约束类：显式失败，带对象名与原因，不静默降级）。"""


def validate_identifier(name: str) -> str:
    """表/列名白名单校验：仅字母数字下划线；非法直接拒绝。"""
    if not _IDENTIFIER_RE.match(name or ""):
        raise StatisticsCollectionError(f"非法标识符（白名单外拒绝）: {name!r}")
    return name


def _sample_subquery(table: str, col: str, limit: int) -> str:
    return f"(SELECT {col} FROM {table} ORDER BY RAND() LIMIT {int(limit)})"


def _hashed_subquery(table: str, col: str, limit: int) -> str:
    """敏感列采样子查询：SQL 侧先哈希，明文值不出库（NULL 经 MD5 仍为 NULL）。"""
    return f"(SELECT MD5({col}) AS v FROM {table} ORDER BY RAND() LIMIT {int(limit)})"


class MysqlStatisticsAdapter:
    """MySQL 统计原语适配器（information_schema + 采样）。"""

    product = "mysql"

    def list_tables(self, q) -> list[str]:
        rows = q(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_NAME",
            (),
            "information_schema.TABLES",
        )
        return [r[0] for r in rows]

    def column_types(self, q, table: str) -> dict[str, str]:
        rows = q(
            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            (table,),
            f"mysql:{table}.columns",
        )
        return {name: (ctype or "").lower() for name, ctype in rows}

    def row_count(self, q, table: str) -> tuple[int | None, str]:
        rows = q(
            "SELECT TABLE_ROWS FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table,),
            f"mysql:{table}.stats",
        )
        nrows = rows[0][0] if rows else None
        return (int(nrows) if nrows is not None else None, "approximate")

    def foreign_keys(self, q, table: str) -> list[tuple[str, str, str]]:
        rows = q(
            "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND REFERENCED_TABLE_NAME IS NOT NULL",
            (table,),
            f"mysql:{table}.foreign_keys",
        )
        return [(r[0], r[1], r[2]) for r in rows]

    # ---------- 采样原语 ----------

    def sample_column(self, q, table: str, col: str, limit: int) -> list:
        rows = q(
            f"SELECT {col} FROM {table} ORDER BY RAND() LIMIT {int(limit)}",
            (),
            f"mysql:{table}.{col}.sample",
        )
        return [r[0] for r in rows]

    def sampled_distinct(self, q, table: str, col: str, limit: int) -> int | None:
        rows = q(
            f"SELECT COUNT(DISTINCT {col}) FROM {_sample_subquery(table, col, limit)} s",
            (),
            f"mysql:{table}.{col}.distinct",
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def sampled_null_ratio(self, q, table: str, col: str, limit: int) -> float | None:
        rows = q(
            f"SELECT AVG({col} IS NULL) FROM {_sample_subquery(table, col, limit)} s",
            (),
            f"mysql:{table}.{col}.null_ratio",
        )
        return float(rows[0][0]) if rows and rows[0][0] is not None else None

    def sampled_top_k(self, q, table: str, col: str, limit: int, k: int) -> list[tuple]:
        rows = q(
            f"SELECT {col}, COUNT(*) AS cnt FROM {_sample_subquery(table, col, limit)} s "
            f"GROUP BY {col} ORDER BY cnt DESC LIMIT {int(k)}",
            (),
            f"mysql:{table}.{col}.top_k",
        )
        return [(r[0], int(r[1])) for r in rows]

    def sampled_top_k_hashed(self, q, table: str, col: str, limit: int, k: int) -> list[tuple]:
        """敏感列：SQL 侧 MD5 哈希分桶，明文不出库（总方案 §4.3）。"""
        rows = q(
            f"SELECT v AS bucket, COUNT(*) AS cnt FROM {_hashed_subquery(table, col, limit)} s "
            f"GROUP BY bucket ORDER BY cnt DESC LIMIT {int(k)}",
            (),
            f"mysql:{table}.{col}.top_k_hashed",
        )
        return [(r[0], int(r[1])) for r in rows]

    def sampled_distinct_hashed(self, q, table: str, col: str, limit: int) -> int | None:
        rows = q(
            f"SELECT COUNT(DISTINCT v) FROM {_hashed_subquery(table, col, limit)} s",
            (),
            f"mysql:{table}.{col}.distinct_hashed",
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def sampled_null_ratio_hashed(self, q, table: str, col: str, limit: int) -> float | None:
        rows = q(
            f"SELECT AVG(v IS NULL) FROM {_hashed_subquery(table, col, limit)} s",
            (),
            f"mysql:{table}.{col}.null_ratio_hashed",
        )
        return float(rows[0][0]) if rows and rows[0][0] is not None else None

    def sampled_length_stats(self, q, table: str, col: str, limit: int) -> tuple | None:
        del limit  # 聚合在 SQL 侧完成（长度统计不泄露取值），直接对表聚合
        rows = q(
            f"SELECT MIN(CHAR_LENGTH({col})), MAX(CHAR_LENGTH({col})), "
            f"AVG(CHAR_LENGTH({col})) FROM {table}",
            (),
            f"mysql:{table}.{col}.length_stats",
        )
        if not rows:
            return None
        mn, mx, avg = rows[0]
        return (mn, mx, float(avg) if avg is not None else None)

    def relation_counts(self, q, child: str, child_col: str,
                        parent: str, parent_col: str, k: int) -> list[tuple]:
        """父键哈希分桶的子行计数（父键值不出库）。"""
        del parent, parent_col  # 关联基数只需子表父键分布
        rows = q(
            f"SELECT MD5({child_col}) AS bucket, COUNT(*) AS cnt FROM {child} "
            f"GROUP BY bucket ORDER BY cnt DESC LIMIT {int(k)}",
            (),
            f"mysql:{child}.{child_col}.relation_counts",
        )
        return [(r[0], int(r[1])) for r in rows]

    def distinct_keys(self, q, child: str, child_col: str) -> int | None:
        rows = q(
            f"SELECT COUNT(DISTINCT {child_col}) FROM {child}",
            (),
            f"mysql:{child}.{child_col}.distinct_keys",
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else None
