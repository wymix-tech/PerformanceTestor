"""AccessPattern 结构化提取（T1.4 收编三个 SQL 抽取器）。

承接模块（lang_java 内副本，legacy 本体不动）：
- `sql_table_extractor.py`   表名/读写类型/JOIN 边/分页 limit
- `sql_predicate_extractor.py` WHERE 谓词（EQ/IN/RANGE）
- `sql_literal_extractor.py` 魔法值字面量与注解 SQL JOIN 关联

输出 `ptde.models.access.AccessPattern`。A3：来源 SQL 无法解析时登记
`unresolved=True`（UNKNOWN）而非丢弃；ORDER BY / GROUP BY 为本层新增的轻量解析。
"""

from __future__ import annotations

import re

from ptde.analyzer.lang_java import sql_literal_extractor as sle
from ptde.analyzer.lang_java import sql_predicate_extractor as spe
from ptde.analyzer.lang_java import sql_table_extractor as ste
from ptde.analyzer.lang_java._legacy_model import Op
from ptde.models.access import AccessPattern, JoinRef, LimitOffset, Predicate, PredicateOp, SqlKind

__all__ = [
    "AccessPatternExtractor",
    "extract_access_patterns",
    "extract_magic_hints",
]

_ORDER_BY = re.compile(r"order\s+by\s+([^\s,()]+(?:\s+(?:asc|desc))?(?:\s*,\s*[^\s,()]+(?:\s+(?:asc|desc))?)*)", re.I)
_GROUP_BY = re.compile(r"group\s+by\s+([^\s,()]+(?:\s*,\s*[^\s,()]+)*)", re.I)
_LIMIT = re.compile(r"limit\s+(\d+)(?:\s+offset\s+(\d+)|\s*,\s*(\d+))?", re.I)

_OP_MAP = {
    Op.EQ: PredicateOp.EQ,
    Op.IN: PredicateOp.IN,
    Op.RANGE: PredicateOp.RANGE,
}


class AccessPatternExtractor:
    """SQL 文本 → AccessPattern（容错：任何解析失败不抛异常）。"""

    def extract(
        self,
        sql: str | None,
        *,
        pattern_id: str,
        sql_kind: str | SqlKind = SqlKind.READ,
        source_ref: str | None = None,
        target_table: str | None = None,
    ) -> AccessPattern:
        kind = SqlKind(sql_kind) if isinstance(sql_kind, str) else sql_kind
        ap = AccessPattern(id=pattern_id, sql_kind=kind)
        if source_ref:
            ap.source_refs.append(source_ref)
        if sql is None or not sql.strip():
            ap.unresolved = True
            ap.unresolved_reason = "empty-sql"
            return ap
        try:
            result = ste.extract(sql)
            ap.tables = result.table_names()
            ap.limit_offset = _extract_limit(sql)
            ap.order_by = _extract_order_by(sql)
            ap.group_by = _extract_group_by(sql)
            for j in ste.extract_join_edges(sql):
                ap.joins.append(
                    JoinRef(
                        child_column=j.child_column,
                        parent_table=j.parent_table,
                        parent_column=j.parent_column,
                        confidence=j.confidence,
                    )
                )
            tt = target_table or (ap.tables[0] if ap.tables else None)
            if tt:
                for p in spe.extract(sql, tt):
                    ap.filters.append(_to_predicate(p))
                    if p.op == Op.RANGE:
                        ap.range_fields.append(p.column or "")
                if kind == SqlKind.READ and ap.filters:
                    ap.time_window = _time_window(ap.filters)
            if not ap.tables and not ap.joins and not ap.filters:
                ap.unresolved = True
                ap.unresolved_reason = result.warning or "no-tables-extracted"
        except Exception as exc:  # noqa: BLE001 容错：登记 UNKNOWN 而非丢弃
            ap.unresolved = True
            ap.unresolved_reason = f"parse-error: {exc}"
        return ap


def extract_access_patterns(sqls: list[tuple[str, str]], *, sql_kind: str = "READ",
                            source_ref: str | None = None,
                            target_table: str | None = None) -> list[AccessPattern]:
    """批量提取（sqls 元素为 (pattern_id, sql)）。"""
    ex = AccessPatternExtractor()
    return [ex.extract(sql, pattern_id=pid, sql_kind=sql_kind, source_ref=source_ref,
                       target_table=target_table) for pid, sql in sqls]


def extract_magic_hints(sql: str | None, target_table: str,
                        source_ref: str | None = None) -> list[dict]:
    """魔法值候选（字面量）→ 结构化 dict（供 Finding.value；T1.5/采纳流消费）。"""
    hints = sle.extract_literals(sql, target_table, source_ref)
    return [
        {
            "table": h.table,
            "column": h.column,
            "kind": h.kind,
            "values": h.values,
            "source_refs": list(h.source_refs),
        }
        for h in hints
    ]


def extract_join_relation_hints(sql: str | None, known_tables) -> list:
    """注解 SQL JOIN 关联边（RelationHint，SRC_ORM_JOIN）→ 交 RelationResolver。

    A3：正则解析结果在 relation_resolver 中一律降为 INFERRED。
    """
    return sle.extract_join_relations(sql, known_tables)


def _to_predicate(p) -> Predicate:
    return Predicate(
        column=p.column or "",
        op=_OP_MAP.get(p.op, PredicateOp.EQ),
        values=list(p.values or []),
        min=p.min,
        max=p.max,
    )


def _extract_limit(sql: str) -> LimitOffset | None:
    m = _LIMIT.search(sql)
    if not m:
        return None
    limit = int(m.group(1))
    offset = int(m.group(2) or m.group(3) or 0)
    # MySQL 语法 LIMIT offset, count → 第一捕获为 offset
    if m.group(3):
        offset, limit = limit, int(m.group(3))
    return LimitOffset(limit=limit, offset=offset)


def _extract_order_by(sql: str) -> list[str]:
    m = _ORDER_BY.search(sql)
    if not m:
        return []
    return [c.strip().strip("`") for c in m.group(1).split(",")]


def _extract_group_by(sql: str) -> list[str]:
    m = _GROUP_BY.search(sql)
    if not m:
        return []
    return [c.strip().strip("`") for c in m.group(1).split(",")]


def _time_window(filters: list[Predicate]) -> dict[str, str] | None:
    """RANGE 谓词中疑似时间字段（created_at/updated_at/*_time/*_date）。"""
    window: dict[str, str] = {}
    for f in filters:
        col = (f.column or "").lower()
        if f.op == PredicateOp.RANGE and (
            col.endswith(("_at", "_time", "_date")) or "time" in col
        ):
            if f.min is not None:
                window.setdefault("from", str(f.min))
            if f.max is not None:
                window.setdefault("to", str(f.max))
    return window or None
