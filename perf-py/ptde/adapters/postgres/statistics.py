"""PostgreSQL 统计采集子适配器（R2 T2.2，新建不改 legacy 方言，A1 分轨）。

采集项对齐实施方案 §5.4 表：PG 优先 pg_stats（n_distinct / null_frac /
most_common_vals+freqs / histogram_bounds），缺失回退采样。
敏感列不读 pg_stats 的 MCV/histogram（原文含明文值），回退 SQL 侧 md5 哈希采样。

约定同 MySQL 适配器：只提供 SQL 原语，全部经采集器只读查询回调 `q` 执行；
标识符拼接前必须过 validate_identifier。
"""

from __future__ import annotations

from ptde.adapters.mysql.statistics import (
    StatisticsCollectionError,
)

__all__ = ["PostgresStatisticsAdapter", "StatisticsCollectionError", "parse_pg_array"]


def parse_pg_array(text: str | None) -> list[str]:
    """解析 PG 数组文本输出（如 '{PAID,COMPLETED}' / '{1,2,3}'），容忍引号包裹。"""
    if not text:
        return []
    body = text.strip().lstrip("{").rstrip("}")
    items: list[str] = []
    cur: list[str] = []
    in_quote = False
    for ch in body:
        if ch == '"':
            in_quote = not in_quote
        elif ch == "," and not in_quote:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur)
    if tail or items:
        items.append(tail)
    return [item for item in items if item != ""]


def _sample_subquery(table: str, col: str, limit: int) -> str:
    return f"(SELECT {col} FROM {table} ORDER BY random() LIMIT {int(limit)})"


def _hashed_subquery(table: str, col: str, limit: int) -> str:
    """敏感列采样子查询：SQL 侧先 md5，明文值不出库（NULL 经 md5 仍为 NULL）。"""
    return f"(SELECT md5({col}::text) AS v FROM {table} ORDER BY random() LIMIT {int(limit)})"


class PostgresStatisticsAdapter:
    """PostgreSQL 统计原语适配器（pg_stats 优先，回退采样）。"""

    product = "postgres"

    def list_tables(self, q) -> list[str]:
        rows = q(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND n.nspname = current_schema() ORDER BY c.relname",
            (),
            "pg_class",
        )
        return [r[0] for r in rows]

    def column_types(self, q, table: str) -> dict[str, str]:
        rows = q(
            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (table,),
            f"postgres:{table}.columns",
        )
        return {name: (ctype or "").lower() for name, ctype in rows}

    def row_count(self, q, table: str) -> tuple[int | None, str]:
        rows = q(
            "SELECT c.reltuples::bigint FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = %s AND n.nspname = current_schema()",
            (table,),
            f"postgres:{table}.stats",
        )
        nrows = rows[0][0] if rows else None
        return (int(nrows) if nrows is not None and nrows >= 0 else None, "approximate")

    def foreign_keys(self, q, table: str) -> list[tuple[str, str, str]]:
        rows = q(
            "SELECT ca.attname, pf.relname, pa.attname "
            "FROM pg_constraint con "
            "JOIN pg_class ch ON ch.oid = con.conrelid "
            "JOIN pg_class pf ON pf.oid = con.confrelid "
            "JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS ck(attnum, ord) ON true "
            "JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS pk(attnum, ord) "
            "ON pk.ord = ck.ord "
            "JOIN pg_attribute ca ON ca.attrelid = con.conrelid AND ca.attnum = ck.attnum "
            "JOIN pg_attribute pa ON pa.attrelid = con.confrelid AND pa.attnum = pk.attnum "
            "WHERE con.contype = 'f' AND ch.relname = %s",
            (table,),
            f"postgres:{table}.foreign_keys",
        )
        return [(r[0], r[1], r[2]) for r in rows]

    def pg_stats(self, q, table: str, col: str) -> dict | None:
        rows = q(
            "SELECT n_distinct, null_frac, most_common_vals::text, "
            "most_common_freqs::text, histogram_bounds::text "
            "FROM pg_stats WHERE schemaname = current_schema() "
            "AND tablename = %s AND attname = %s",
            (table, col),
            f"postgres:{table}.{col}.pg_stats",
        )
        if not rows:
            return None
        n_distinct, null_frac, mcv, mcf, hist = rows[0]
        return {
            "n_distinct": float(n_distinct) if n_distinct is not None else None,
            "null_frac": float(null_frac) if null_frac is not None else None,
            "mcv": parse_pg_array(mcv),
            "mcv_freqs": [float(f) for f in parse_pg_array(mcf)],
            "histogram_bounds": parse_pg_array(hist),
        }

    # ---------- 采样原语（pg_stats 缺失时的回退） ----------

    def sample_column(self, q, table: str, col: str, limit: int) -> list:
        rows = q(
            f"SELECT {col} FROM {table} ORDER BY random() LIMIT {int(limit)}",
            (),
            f"postgres:{table}.{col}.sample",
        )
        return [r[0] for r in rows]

    def sampled_distinct(self, q, table: str, col: str, limit: int) -> int | None:
        """pg_stats.n_distinct 优先（非负即绝对基数），缺失回退采样。"""
        stats = self.pg_stats(q, table, col)
        if stats and stats["n_distinct"] is not None and stats["n_distinct"] >= 0:
            return int(round(stats["n_distinct"]))
        rows = q(
            f"SELECT COUNT(DISTINCT {col}) FROM {_sample_subquery(table, col, limit)} s",
            (),
            f"postgres:{table}.{col}.distinct",
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def sampled_null_ratio(self, q, table: str, col: str, limit: int) -> float | None:
        """pg_stats.null_frac 优先，缺失回退采样。"""
        stats = self.pg_stats(q, table, col)
        if stats and stats["null_frac"] is not None:
            return stats["null_frac"]
        rows = q(
            f"SELECT AVG(CASE WHEN {col} IS NULL THEN 1.0 ELSE 0.0 END) "
            f"FROM {_sample_subquery(table, col, limit)} s",
            (),
            f"postgres:{table}.{col}.null_ratio",
        )
        return float(rows[0][0]) if rows and rows[0][0] is not None else None

    def sampled_top_k(self, q, table: str, col: str, limit: int, k: int) -> list[tuple]:
        """pg_stats.most_common_vals/freqs 优先（频次×样本量换算），缺失回退采样。"""
        stats = self.pg_stats(q, table, col)
        if stats and stats["mcv"] and stats["mcv_freqs"]:
            return [
                (val, int(round(freq * limit)))
                for val, freq in zip(stats["mcv"], stats["mcv_freqs"], strict=False)
            ][:k]
        rows = q(
            f"SELECT {col}, COUNT(*) AS cnt FROM {_sample_subquery(table, col, limit)} s "
            f"GROUP BY {col} ORDER BY cnt DESC LIMIT {int(k)}",
            (),
            f"postgres:{table}.{col}.top_k",
        )
        return [(r[0], int(r[1])) for r in rows]

    def sampled_top_k_hashed(self, q, table: str, col: str, limit: int, k: int) -> list[tuple]:
        """敏感列：SQL 侧 md5 哈希分桶，明文不出库（总方案 §4.3）。"""
        rows = q(
            f"SELECT v AS bucket, COUNT(*) AS cnt FROM {_hashed_subquery(table, col, limit)} s "
            f"GROUP BY bucket ORDER BY cnt DESC LIMIT {int(k)}",
            (),
            f"postgres:{table}.{col}.top_k_hashed",
        )
        return [(r[0], int(r[1])) for r in rows]

    def sampled_distinct_hashed(self, q, table: str, col: str, limit: int) -> int | None:
        rows = q(
            f"SELECT COUNT(DISTINCT v) FROM {_hashed_subquery(table, col, limit)} s",
            (),
            f"postgres:{table}.{col}.distinct_hashed",
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def sampled_null_ratio_hashed(self, q, table: str, col: str, limit: int) -> float | None:
        rows = q(
            f"SELECT AVG(CASE WHEN v IS NULL THEN 1.0 ELSE 0.0 END) "
            f"FROM {_hashed_subquery(table, col, limit)} s",
            (),
            f"postgres:{table}.{col}.null_ratio_hashed",
        )
        return float(rows[0][0]) if rows and rows[0][0] is not None else None

    def sampled_length_stats(self, q, table: str, col: str, limit: int) -> tuple | None:
        del limit  # 聚合在 SQL 侧完成（长度统计不泄露取值），直接对表聚合
        rows = q(
            f"SELECT MIN(LENGTH({col})), MAX(LENGTH({col})), AVG(LENGTH({col})) "
            f"FROM {table}",
            (),
            f"postgres:{table}.{col}.length_stats",
        )
        if not rows:
            return None
        mn, mx, avg = rows[0]
        return (mn, mx, float(avg) if avg is not None else None)

    def relation_counts(self, q, child: str, child_col: str,
                        parent: str, parent_col: str, k: int) -> list[tuple]:
        del parent, parent_col
        rows = q(
            f"SELECT md5({child_col}::text) AS bucket, COUNT(*) AS cnt FROM {child} "
            f"GROUP BY bucket ORDER BY cnt DESC LIMIT {int(k)}",
            (),
            f"postgres:{child}.{child_col}.relation_counts",
        )
        return [(r[0], int(r[1])) for r in rows]

    def distinct_keys(self, q, child: str, child_col: str) -> int | None:
        rows = q(
            f"SELECT COUNT(DISTINCT {child_col}) FROM {child}",
            (),
            f"postgres:{child}.{child_col}.distinct_keys",
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else None
