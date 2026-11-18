"""SQL WHERE 谓词抽取器：移植自 util/SqlPredicateExtractor.java。

产出直接驱动「数据如何构成」——等值谓词转枚举值集、范围谓词转区间分布。
只有带字面量的谓词才有数据构造价值；status = #{status} 这类占位符谓词天然
不被正则命中，无需特殊处理。

列归属：优先认表限定谓词（o.status = 1，别名须属于目标表）；当 SQL 中不存在
任何表别名时，才额外接受非限定谓词（status = 1）。

任何异常静默返回，绝不抛出中断分析。
"""

from __future__ import annotations

import re

from ptde.analyzer.lang_java._legacy_model import Op, Predicate
from ptde.analyzer.lang_java.sql_table_extractor import is_keyword_candidate

_EQ_STR = re.compile(r"([\w]+)\.([\w`]+)\s*=\s*'([^']*)'", re.A)
_EQ_NUM = re.compile(r"([\w]+)\.([\w`]+)\s*=\s*(\d+)", re.A)
_IN_LIST = re.compile(r"([\w]+)\.([\w`]+)\s+in\s*\(([^)]*)\)", re.I | re.A)
_RANGE = re.compile(r"([\w]+)\.([\w`]+)\s*(>=|<=|>|<)\s*(\d+(?:\.\d+)?)", re.A)
_BETWEEN = re.compile(
    r"([\w]+)\.([\w`]+)\s+between\s+(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)",
    re.I | re.A,
)

# 非限定列谓词（无表别名时使用）
_BARE_EQ_STR = re.compile(r"(?<![\w.])([\w`]+)\s*=\s*'([^']*)'", re.A)
_BARE_EQ_NUM = re.compile(r"(?<![\w.])([\w`]+)\s*=\s*(\d+)", re.A)
_BARE_IN = re.compile(r"(?<![\w.])([\w`]+)\s+in\s*\(([^)]*)\)", re.I | re.A)
_BARE_RANGE = re.compile(r"(?<![\w.])([\w`]+)\s*(>=|<=|>|<)\s*(\d+(?:\.\d+)?)", re.A)

# 表别名：from/join t1 a
_TABLE_ALIAS = re.compile(r"(?:from|join)\s+([`\w.]+)\s+(?:as\s+)?([\w]+)", re.I | re.A)

_QUOTED = re.compile(r"'([^']*)'")


def extract(sql: str | None, target_table: str | None) -> list[Predicate]:
    """抽取目标表的谓词（去重按列 + 算子）。"""
    out: list[Predicate] = []
    if sql is None or not sql.strip() or target_table is None:
        return out
    try:
        table = target_table.lower()
        allowed = _aliases_of(sql, table)

        # 表限定谓词
        _match_eq(out, sql, _EQ_STR, allowed, False)
        _match_eq(out, sql, _EQ_NUM, allowed, True)
        _match_in(out, sql, _IN_LIST, allowed)
        _match_range(out, sql, _RANGE, allowed)
        _match_between(out, sql, _BETWEEN, allowed)

        # 无别名时才接受非限定谓词（避免把别的表/别名列的谓词算到本表头上）
        if not has_any_alias(sql):
            _match_bare_eq(out, sql, _BARE_EQ_STR, False)
            _match_bare_eq(out, sql, _BARE_EQ_NUM, True)
            _match_bare_in(out, sql, _BARE_IN)
            _match_bare_range(out, sql, _BARE_RANGE)
    except Exception:  # noqa: BLE001 容错：静默
        pass
    return out


def has_any_alias(sql: str) -> bool:
    """SQL 中是否存在表别名（from t a / join t b）。"""
    for m in _TABLE_ALIAS.finditer(sql.lower()):
        if not is_keyword(m.group(2)):
            return True
    return False


def _aliases_of(sql: str, table: str) -> set[str]:
    """目标表允许出现的限定符集合：表名自身 + 绑定到该表的别名。"""
    allowed = {table}
    for m in _TABLE_ALIAS.finditer(sql.lower()):
        raw_table = m.group(1).replace("`", "")
        dot = raw_table.rfind(".")
        simple = raw_table[dot + 1:] if dot >= 0 else raw_table
        if simple == table:
            allowed.add(m.group(2))
    return allowed


def _match_eq(out: list[Predicate], sql: str, p: re.Pattern[str],
              allowed: set[str], numeric: bool) -> None:
    for m in p.finditer(sql):
        if m.group(1).lower() not in allowed:
            continue
        v: object | None = int(m.group(3)) if numeric else m.group(3)
        if v is None:
            continue
        _add_eq(out, _column(m.group(2)), v)


def _match_in(out: list[Predicate], sql: str, p: re.Pattern[str], allowed: set[str]) -> None:
    for m in p.finditer(sql):
        if m.group(1).lower() not in allowed:
            continue
        values = _parse_value_list(m.group(3))
        if not values:
            continue
        pred = Predicate(column=_column(m.group(2)), op=Op.IN)
        pred.values.extend(values)
        out.append(pred)


def _match_range(out: list[Predicate], sql: str, p: re.Pattern[str], allowed: set[str]) -> None:
    for m in p.finditer(sql):
        if m.group(1).lower() not in allowed:
            continue
        v = _to_double(m.group(4))
        if v is None:
            continue
        pred = Predicate(column=_column(m.group(2)), op=Op.RANGE)
        if m.group(3) in (">", ">="):
            pred.min = v
        else:
            pred.max = v
        out.append(pred)


def _match_between(out: list[Predicate], sql: str, p: re.Pattern[str], allowed: set[str]) -> None:
    for m in p.finditer(sql):
        if m.group(1).lower() not in allowed:
            continue
        lo = _to_double(m.group(3))
        hi = _to_double(m.group(4))
        if lo is None or hi is None:
            continue
        pred = Predicate(column=_column(m.group(2)), op=Op.RANGE, min=lo, max=hi)
        out.append(pred)


def _match_bare_eq(out: list[Predicate], sql: str, p: re.Pattern[str], numeric: bool) -> None:
    for m in p.finditer(sql):
        col = _column(m.group(1))
        if is_keyword(col):
            continue
        v: object | None = int(m.group(2)) if numeric else m.group(2)
        if v is None:
            continue
        _add_eq(out, col, v)


def _match_bare_in(out: list[Predicate], sql: str, p: re.Pattern[str]) -> None:
    for m in p.finditer(sql):
        col = _column(m.group(1))
        if is_keyword(col):
            continue
        values = _parse_value_list(m.group(2))
        if not values:
            continue
        pred = Predicate(column=col, op=Op.IN)
        pred.values.extend(values)
        out.append(pred)


def _match_bare_range(out: list[Predicate], sql: str, p: re.Pattern[str]) -> None:
    for m in p.finditer(sql):
        col = _column(m.group(1))
        if is_keyword(col):
            continue
        v = _to_double(m.group(3))
        if v is None:
            continue
        pred = Predicate(column=col, op=Op.RANGE)
        if m.group(2) in (">", ">="):
            pred.min = v
        else:
            pred.max = v
        out.append(pred)


def _add_eq(out: list[Predicate], col: str, value: object) -> None:
    for p in out:
        if p.op == Op.EQ and col == p.column:
            if value not in p.values:
                p.values.append(value)
            return
    pred = Predicate(column=col, op=Op.EQ)
    pred.values.append(value)
    out.append(pred)


def _parse_value_list(raw: str) -> list[object]:
    """解析 IN 列表：优先字符串字面量，其次数值。"""
    values: list[object] = []
    quoted = _QUOTED.findall(raw)
    if quoted:
        values.extend(quoted)
        return values
    for part in raw.split(","):
        s = part.strip()
        if re.fullmatch(r"\d+", s):
            values.append(int(s))
    return values


def _column(raw: str | None) -> str:
    return "" if raw is None else raw.replace("`", "").lower()


def _to_number(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _to_double(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def is_keyword(s: str | None) -> bool:
    return is_keyword_candidate(s)
