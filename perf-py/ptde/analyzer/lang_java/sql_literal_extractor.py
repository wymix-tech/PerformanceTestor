"""SQL 文本容错抽取器：移植自 util/SqlLiteralExtractor.java。

从 SQL 字符串中提取魔法值字面量与 JOIN 关联。
任何解析失败静默返回空结果，绝不抛出异常中断灌数（SQL 碎片化防御）。
"""

from __future__ import annotations

import re

from ptde.analyzer.lang_java._legacy_model import MagicHint, RelationHint

# 相等谓词：a.col = 'v' / a.col = 123（别名.列名.值 三段捕获）
_EQ_STR = re.compile(r"([\w]+)\.([\w`]+)\s*=\s*'([^']*)'", re.A)
_EQ_NUM = re.compile(r"([\w]+)\.([\w`]+)\s*=\s*(\d+)", re.A)
# IN 列表：a.col IN (1,2) / a.col IN ('a','b')
_IN_LIST = re.compile(r"([\w]+)\.([\w`]+)\s+in\s*\(([^)]*)\)", re.I | re.A)
_QUOTED = re.compile(r"'([^']*)'")
# JOIN ON：join t2 b on a.col1 = b.col2（左连接同样命中）
_JOIN_ON = re.compile(
    r"join\s+([\w]+)\s+([\w]+)\s+on\s+([\w]+)\.([\w`]+)\s*=\s*([\w]+)\.([\w`]+)",
    re.I | re.A,
)
# 表别名：from/join t1 a（JOIN 子句别名同样进入映射）
_TABLE_ALIAS = re.compile(r"(?:from|join)\s+([\w]+)\s+(?:as\s+)?([\w]+)", re.I | re.A)


def extract_literals(sql: str | None, target_table: str | None,
                     source_ref: str | None) -> list[MagicHint]:
    """从 SQL 提取指定表的魔法值候选。

    targetTable 小写；列名归属需有表限定符命中该表或其别名。
    """
    hints: list[MagicHint] = []
    if sql is None or not sql.strip() or target_table is None:
        return hints
    try:
        t = target_table.lower()
        allowed: set[str] = {t}
        for ta in _TABLE_ALIAS.finditer(sql):
            if ta.group(1).lower() == t:
                allowed.add(ta.group(2).lower())
        # 相等谓词（字符串/数值）
        _add_eq(hints, sql, _EQ_STR, allowed, target_table, source_ref, False)
        _add_eq(hints, sql, _EQ_NUM, allowed, target_table, source_ref, True)
        # IN 列表
        for in_m in _IN_LIST.finditer(sql):
            if in_m.group(1).lower() not in allowed:
                continue
            values: list[object] = []
            quoted = _QUOTED.findall(in_m.group(3))
            if quoted:
                values.extend(quoted)
            else:
                for part in in_m.group(3).split(","):
                    p = part.strip()
                    if re.fullmatch(r"\d+", p):
                        values.append(int(p))
            _add_hint(hints, target_table, in_m.group(2), values, source_ref)
    except Exception:  # noqa: BLE001 容错：静默
        pass
    return hints


def _add_eq(hints: list[MagicHint], sql: str, p: re.Pattern[str], allowed: set[str],
            target_table: str, source_ref: str | None, numeric: bool) -> None:
    """相等谓词收集：group1=别名（须命中 allowed）、group2=列名、group3=值。"""
    for m in p.finditer(sql):
        if m.group(1).lower() not in allowed:
            continue
        try:
            v: object = int(m.group(3)) if numeric else m.group(3)
            _add_hint(hints, target_table, m.group(2), [v], source_ref)
        except ValueError:  # 容错：静默
            pass


def _add_hint(hints: list[MagicHint], table: str, column: str,
              values: list[object], source_ref: str | None) -> None:
    h = MagicHint(table, column.replace("`", ""), MagicHint.KIND_LITERAL)
    h.values.extend(values)
    if source_ref is not None:
        h.source_refs.append(source_ref)
    hints.append(h)


def extract_join_relations(sql: str | None, known_tables) -> list[RelationHint]:
    """从 JOIN ON 提取关联边（归属 known_tables 内的表）。"""
    hints: list[RelationHint] = []
    if sql is None or not sql.strip():
        return hints
    try:
        lower = sql.lower()
        known = {t.lower() for t in known_tables}
        # alias → table（from 与 join 子句的别名均进入映射）
        alias_table: dict[str, str] = {}
        for ta in _TABLE_ALIAS.finditer(lower):
            alias_table[ta.group(2)] = ta.group(1)
        for j in _JOIN_ON.finditer(lower):
            t1 = alias_table.get(j.group(3), j.group(3))
            t2 = alias_table.get(j.group(5), j.group(5))
            if t1 not in known or t2 not in known or t1 == t2:
                continue
            c1 = j.group(4).replace("`", "")
            c2 = j.group(6).replace("`", "")
            # 约定：含 _id 后缀的列一侧为子表
            h = _build_edge(t1, c1, t2, c2)
            if h is not None:
                h.source_refs.append("sql-join(regex)")
                hints.append(h)
    except Exception:  # noqa: BLE001 容错：静默
        pass
    return hints


def _build_edge(t1: str, c1: str, t2: str, c2: str) -> RelationHint | None:
    c1_is_fk = c1.endswith("_id")
    c2_is_fk = c2.endswith("_id")
    if c1_is_fk and not c2_is_fk:
        return RelationHint(t1, c1, t2, c2, RelationHint.SRC_ORM_JOIN, 0.9)
    if c2_is_fk and not c1_is_fk:
        return RelationHint(t2, c2, t1, c1, RelationHint.SRC_ORM_JOIN, 0.9)
    # 两端皆含/皆不含 _id：按表名匹配列前缀判定（user_id ↔ user.id）
    # 注意对齐 Java String.replace：移除所有 "_id" 出现（非仅后缀）
    if t2 == c1.replace("_id", ""):
        return RelationHint(t1, c1, t2, c2, RelationHint.SRC_ORM_JOIN, 0.9)
    if t1 == c2.replace("_id", ""):
        return RelationHint(t2, c2, t1, c1, RelationHint.SRC_ORM_JOIN, 0.9)
    return None
