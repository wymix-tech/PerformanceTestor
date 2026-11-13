"""枚举值四级发现：移植自 enumv/EnumValueDiscoverer.java。

优先级递减：
1. MySQL ENUM 列类型（TYPE_NAME 解析，方言 getEnumValues 承接）
2. CHECK 约束（information_schema，MySQL 8.0.16+ 通用 SQL）
3. 列注释模式「状态 1=待支付 2=已支付」正则解析
4. Java 枚举反查（MyBatis 源码扫描产物，列名 ↔ 枚举类名模糊匹配）

全部来源均未命中时返回空（引擎维持类型默认生成，并将列标记为疑似枚举列交前端提示）。
"""

from __future__ import annotations

import re

from datagen.model import ColumnMeta

# 注释枚举模式：「1=待支付 2=已支付」/「PENDING=待支付」键值对（容忍全角标点分隔）
COMMENT_PAIR = re.compile(r"([A-Za-z0-9_\-]+)\s*[=＝]\s*([^\s,;，；、]+)")
# CHECK 约束中 col IN ('a','b') 或 col = 'a' 的字面量提取
CHECK_IN = re.compile(r"[\w`\"\[\]]+\s+in\s*\(([^)]+)\)", re.I)
CHECK_EQ = re.compile(r"[\w`\"\[\]]+\s*=\s*'([^']*)'")
QUOTED = re.compile(r"'([^']*)'")
# 疑似枚举列名（值发现失败时提示前端人工确认）
SUSPECT_NAME = re.compile(r".*(status|state|type|flag|level|kind|category).*")


def discover(col: ColumnMeta, check_clause: str | None = None,
             java_enums: dict[str, list[str]] | None = None) -> list[object]:
    """无 DB 重载：宿主可预取 information_schema.CHECK_CONSTRAINTS 后传入 check_clause。"""
    # 级别 1 内联 ENUM
    inline = parse_inline_enum(col.type_name)
    if inline:
        return list(inline)
    # 级别 2：CHECK 约束字面量
    from_check = parse_check_clause(check_clause)
    if from_check:
        return from_check
    # 级别 3：注释
    from_comment = parse_comment(col.comment, col)
    if from_comment:
        return from_comment
    # 级别 4：Java 枚举
    if java_enums:
        candidates = match_java_enum(col.name, java_enums)
        if candidates:
            return list(candidates)
    return []


def discover_with_db(conn, dialect, table_name: str | None, col: ColumnMeta,
                     java_enums: dict[str, list[str]] | None = None) -> list[object]:
    """带表名的枚举发现（DB 级查询需要表名：MySQL information_schema / PG pg_enum）。

    table_name 为空则跳过 DB 级查询；方言查询失败降级后续来源。
    """
    # 级别 1：DB ENUM 类型（MySQL TYPE_NAME=ENUM('A','B')；PG pg_enum/CHECK 由方言实现）
    try:
        if conn is not None and dialect is not None and table_name and table_name.strip():
            db_vals = dialect.get_enum_values(conn, table_name, col.name)
            if db_vals:
                return list(db_vals)
    except Exception:  # noqa: BLE001 方言查询失败降级后续来源
        pass
    # TYPE_NAME 内联 ENUM 解析（不依赖连接）
    inline = parse_inline_enum(col.type_name)
    if inline:
        return list(inline)
    # 级别 3：列注释
    from_comment = parse_comment(col.comment, col)
    if from_comment:
        return from_comment
    # 级别 4：Java 枚举反查
    if java_enums:
        candidates = match_java_enum(col.name, java_enums)
        if candidates:
            return list(candidates)
    return []


def parse_inline_enum(type_name: str | None) -> list[str]:
    """MySQL ENUM('A','B') 类型名解析。"""
    vals: list[str] = []
    if type_name is None:
        return vals
    t = type_name.strip()
    upper = t.upper()
    if not upper.startswith("ENUM(") and not upper.startswith("SET("):
        return vals
    vals.extend(QUOTED.findall(t))
    return vals


def parse_check_clause(clause: str | None) -> list[object]:
    """CHECK 子句字面量提取：col IN ('a','b') / col = 'x' / 数值列 IN (1,2)。"""
    vals: list[object] = []
    if clause is None or not clause.strip():
        return vals
    segment: str | None = None
    for in_m in CHECK_IN.finditer(clause):
        segment = in_m.group(1)
        break
    if segment is None:
        for eq in CHECK_EQ.finditer(clause):
            vals.append(eq.group(1))
        return vals
    quoted = QUOTED.findall(segment)
    if quoted:
        vals.extend(quoted)
    else:
        for part in segment.split(","):
            p = part.strip()
            if p:
                vals.append(_parse_number(p))
    return vals


def parse_comment(comment: str | None, col: ColumnMeta) -> list[object]:
    """注释键值对解析：整数族列取键（数字）；字符串列键为字母数字单词时取键。"""
    vals: list[object] = []
    if comment is None or not comment.strip():
        return vals
    found = 0
    for m in COMMENT_PAIR.finditer(comment):
        key = m.group(1)
        vals.append(_parse_number(key))
        found += 1
    # 至少 2 对才视为枚举注释，避免误报
    if found < 2:
        return []
    return vals


def match_java_enum(column_name: str | None,
                    java_enums: dict[str, list[str]]) -> list[str]:
    """列名 ↔ 枚举类名模糊匹配：order_status → OrderStatus / UserState → user_state。"""
    if column_name is None:
        return []
    col = column_name.lower()
    for cls, values in java_enums.items():
        # 类名 → snake_case
        snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", cls).lower()
        if (col == snake or col.endswith("_" + snake) or snake.endswith("_" + col)
                or col.replace("_", "") == snake.replace("_", "")):
            return values
    return []


def suspect_enum_column(col: ColumnMeta) -> bool:
    """疑似枚举列（值发现失败但列名含 status/type/state/flag 等）。"""
    n = (col.name or "").lower()
    return bool(SUSPECT_NAME.fullmatch(n))


def _parse_number(s: str) -> object:
    try:
        return int(s)
    except ValueError:
        return s
