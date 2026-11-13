"""SQL 表名与读写类型抽取器：移植自 util/SqlTableExtractor.java。

原则「宁缺勿滥」：只接受确定可判定的形态（单表 FROM t / JOIN t / UPDATE t /
INSERT INTO t，可带反引号与库名前缀）。遇到子查询、CTE、多表 UPDATE 等不完整
形态时不猜：已确定的表照常给出，同时在 warning 写入原因，由前端黄色提示引导
手工补表。理由：对错误的表造数据（可能污染业务表）危害远大于漏表。

任何异常静默返回空结果，绝不抛出中断分析。

正则移植说明：Java \\w 默认 ASCII，Python 需显式加 re.A 保持等价。
"""

from __future__ import annotations

import re

from datagen.model import SqlAccess

# 表名候选（含可选库名前缀与反引号）
_TBL = r"([`\w.]+)"

_FROM = re.compile(r"\bfrom\s+" + _TBL, re.I | re.A)
_JOIN = re.compile(r"\bjoin\s+" + _TBL, re.I | re.A)
_INTO = re.compile(r"\binto\s+" + _TBL, re.I | re.A)
_UPDATE = re.compile(r"\bupdate\s+" + _TBL, re.I | re.A)

_LEADING_VERB = re.compile(r"^\s*(select|insert|update|delete|replace)\b", re.I | re.A)
_LEADING_DELETE_FROM = re.compile(r"^\s*delete\s+from\b", re.I | re.A)
# 多表 UPDATE：update a, b set ...（表可带别名）
_MULTI_TABLE_UPDATE = re.compile(r"\bupdate\s+[`\w.]+(?:\s+(?:as\s+)?\w+)?\s*,", re.I | re.A)
_SUBQUERY = re.compile(r"\b(?:from|join)\s*\(", re.I | re.A)
_CTE = re.compile(r"\bwith\s+[\w`]+\s+as\s*\(", re.I | re.A)
_LIMIT = re.compile(r"\blimit\s+(\d+)", re.I | re.A)

# from t [as] a
_FROM_ALIAS = re.compile(r"\bfrom\s+([`\w.]+)(?:\s+(?:as\s+)?(\w+))?", re.I | re.A)
# join t [as] b
_JOIN_ALIAS = re.compile(r"\bjoin\s+([`\w.]+)(?:\s+(?:as\s+)?(\w+))?", re.I | re.A)
# join t [as] b on x.col = y.col
_JOIN_ON = re.compile(
    r"\bjoin\s+[`\w.]+(?:\s+(?:as\s+)?\w+)?\s+on\s+"
    r"(\w+)\.([\w`]+)\s*=\s*(\w+)\.([\w`]+)",
    re.I | re.A,
)
# 限定列引用：alias.column
_QUALIFIED_COLUMN = re.compile(r"(?<![\w.`])(\w+)\.([\w`]+)", re.A)

# 合法表名（小写后校验），用于剔除误吸的 SQL 关键字与符号片段
_VALID_TABLE = re.compile(r"[a-z_][a-z0-9_]{0,63}")

# 误吸关键字黑名单。注意：表名 order 是业务常见表名，不入黑名单——
# 它只可能出现在 from/join/update/into 之后，此时它就是表名而非 ORDER BY 子句。
SQL_KEYWORDS = frozenset({
    "select", "from", "where", "and", "or", "not", "null", "set", "values", "value", "on", "using",
    "having", "group", "limit", "offset", "union", "all", "distinct", "as", "join", "inner",
    "left", "right", "outer", "cross", "into", "update", "delete", "insert", "case", "when", "then",
    "else", "end", "like", "between", "in", "exists", "asc", "desc", "by", "with", "recursive",
    "for", "duplicate", "key", "ignore", "straight_join", "natural", "force", "use", "index",
})


class TableAccess:
    """单表访问：表名 + 读写类型。"""

    def __init__(self, table: str, access: SqlAccess) -> None:
        self.table = table
        self.access = access

    def __str__(self) -> str:
        return f"{self.table}/{self.access.value}"


class ExtractResult:
    """抽取结果。"""

    def __init__(self) -> None:
        self.tables: list[TableAccess] = []
        # 非空表示结论可能不完整（仅提示，不阻断）
        self.warning: str | None = None
        # SQL 中的 limit 字面量（0 表示未识别）
        self.page_size: int = 0

    def table_names(self) -> list[str]:
        """表名集合（去重，保持出现顺序）。"""
        seen: list[str] = []
        for t in self.tables:
            if t.table not in seen:
                seen.append(t.table)
        return seen


class JoinEdge:
    """JOIN 关联边：子表外键 → 父表列。"""

    def __init__(self, child_table: str, child_column: str,
                 parent_table: str, parent_column: str, confidence: float) -> None:
        self.child_table = child_table
        self.child_column = child_column
        self.parent_table = parent_table
        self.parent_column = parent_column
        self.confidence = confidence

    def __str__(self) -> str:
        return (f"{self.child_table}.{self.child_column} → "
                f"{self.parent_table}.{self.parent_column} (conf={self.confidence})")


def extract(sql: str | None) -> ExtractResult:
    """抽取 SQL 中的表名与读写类型（不抛异常；无命中时 tables 为空）。"""
    result = ExtractResult()
    if sql is None or not sql.strip():
        return result
    try:
        text = sql
        verb = _LEADING_VERB.search(text)
        leading = verb.group(1).lower() if verb else ""
        delete_from = bool(_LEADING_DELETE_FROM.search(text))

        lim = _LIMIT.search(text)
        if lim:
            try:
                result.page_size = int(lim.group(1))
            except ValueError:
                pass  # 超大字面量：按未识别处理

        # INTO / UPDATE 目标恒为写；DELETE FROM 的 from 目标为写；其余 from/join 为读
        _collect(text, _INTO, SqlAccess.WRITE, result)
        _collect(text, _UPDATE, SqlAccess.WRITE, result)
        _collect(text, _FROM, SqlAccess.WRITE if delete_from else SqlAccess.READ, result)
        _collect(text, _JOIN, SqlAccess.READ, result)

        # 写语句中经 FROM 读到的表仍可能是真实读取（INSERT ... SELECT），此处不做改写
        if leading in ("insert", "update", "delete", "replace"):
            # 写语句里若没有任何 WRITE 目标（如动态 SQL 缺 into），降级提示
            if not any(t.access == SqlAccess.WRITE for t in result.tables):
                result.warning = "写语句未识别到写入目标表（动态 SQL 碎片？）"

        if _SUBQUERY.search(text):
            result.warning = _merge(result.warning, "含子查询/派生表，涉及表可能不完整")
        if _CTE.search(text):
            result.warning = _merge(result.warning, "含 CTE（WITH ... AS），涉及表可能不完整")
        if _MULTI_TABLE_UPDATE.search(text):
            result.warning = _merge(result.warning, "多表 UPDATE 未完整解析，仅取首个表")
    except Exception:  # noqa: BLE001 容错：静默
        pass
    return result


def _collect(sql: str, p: re.Pattern[str], access: SqlAccess, out: ExtractResult) -> None:
    for m in p.finditer(sql):
        table = sanitize(m.group(1))
        if table is None:
            continue
        if any(t.table == table for t in out.tables):
            continue
        out.tables.append(TableAccess(table, access))


def is_keyword_candidate(s: str | None) -> bool:
    """是否为 SQL 关键字（供同类抽取器复用，避免把关键字误判为列名/别名）。"""
    if s is None:
        return True
    v = s.replace("`", "").strip().lower()
    return not v or v in SQL_KEYWORDS


def sanitize(raw: str | None) -> str | None:
    """表名清洗：去反引号、去库名前缀、小写、剔除关键字与非法名。"""
    if raw is None:
        return None
    s = raw.replace("`", "").strip()
    dot = s.rfind(".")
    if dot >= 0:
        s = s[dot + 1:]
    s = s.lower()
    if not s or s in SQL_KEYWORDS or not _VALID_TABLE.fullmatch(s):
        return None
    return s


def _merge(a: str | None, b: str) -> str:
    if a is None or not a.strip():
        return b
    return a + "；" + b


# ===== JOIN 关联边与限定列抽取（供外键引用推导使用） =====

def _alias_map(sql: str) -> dict[str, str]:
    """表别名 → 表名（小写）。"""
    alias_map: dict[str, str] = {}
    fm = _FROM_ALIAS.search(sql)
    if fm:
        table = sanitize(fm.group(1))
        if table is not None:
            alias = (table if fm.group(2) is None or is_keyword_candidate(fm.group(2))
                     else fm.group(2).lower())
            alias_map[alias] = table
    for jm in _JOIN_ALIAS.finditer(sql):
        table = sanitize(jm.group(1))
        if table is None:
            continue
        alias = (table if jm.group(2) is None or is_keyword_candidate(jm.group(2))
                 else jm.group(2).lower())
        if alias not in alias_map:
            alias_map[alias] = table
    return alias_map


def extract_join_edges(sql: str | None) -> list[JoinEdge]:
    """抽取 JOIN ON 关联边（子表外键 → 父表主键）。

    方向判定优先级：外键命名证据（user_id → perf_user）→ 主键侧证据（= p.id）
    → 兜底「FROM 侧为子表」。前两级置信度 0.9/0.7，兜底仅 0.5。
    """
    out: list[JoinEdge] = []
    if sql is None or not sql.strip():
        return out
    try:
        alias_map = _alias_map(sql)
        if not alias_map:
            return out
        from_table = next(iter(alias_map.values()))
        for m in _JOIN_ON.finditer(sql):
            la = m.group(1).lower()
            lc = m.group(2).replace("`", "").lower()
            ra = m.group(3).lower()
            rc = m.group(4).replace("`", "").lower()
            lt = alias_map.get(la)
            rt = alias_map.get(ra)
            if lt is None or rt is None or lt == rt:
                continue
            left_fk = is_fk_column(lc, rt)
            right_fk = is_fk_column(rc, lt)
            if left_fk and not right_fk:
                left_is_child, conf = True, 0.9
            elif right_fk and not left_fk:
                left_is_child, conf = False, 0.9
            elif is_id_column(rc) and not is_id_column(lc):
                left_is_child, conf = True, 0.7
            elif is_id_column(lc) and not is_id_column(rc):
                left_is_child, conf = False, 0.7
            else:
                left_is_child, conf = lt == from_table, 0.5
            child_table = lt if left_is_child else rt
            child_column = lc if left_is_child else rc
            parent_table = rt if left_is_child else lt
            parent_column = rc if left_is_child else lc
            out.append(JoinEdge(child_table, child_column, parent_table, parent_column, conf))
    except Exception:  # noqa: BLE001 容错：静默
        pass
    return out


def extract_columns_by_table(sql: str | None) -> dict[str, set[str]]:
    """表名 → 该表在 SQL 中被引用到的列名集合。

    多表时只收限定列（o.status）；单表时额外收非限定列（N+1 形态常见
    WHERE user_id = #{userId}，没有别名前缀）。单表场景歧义低，接收非限定列
    是安全且必要的。
    """
    out: dict[str, set[str]] = {}
    if sql is None or not sql.strip():
        return out
    try:
        alias_map = _alias_map(sql)
        if not alias_map:
            return out
        for m in _QUALIFIED_COLUMN.finditer(sql):
            alias = m.group(1).lower()
            table = alias_map.get(alias)
            if table is None:
                continue
            col = m.group(2).replace("`", "").lower()
            if not col or is_keyword_candidate(col):
                continue
            out.setdefault(table, set()).add(col)
        if len(alias_map) == 1:
            cols = out.setdefault(next(iter(alias_map.values())), set())
            cols.update(extract_bare_columns(sql))
    except Exception:  # noqa: BLE001 容错：静默
        pass
    return out


def extract_bare_columns(sql: str) -> set[str]:
    """单表 SQL 的非限定列名：比较运算符左侧、IN/BETWEEN/LIKE 左侧、以及 SET 左侧。

    只取标识符形态并剔除关键字、数字、字符串与函数调用，误收风险低。
    """
    out: list[str] = []
    p = re.compile(r"(?<![\w.`])(\w+)\s*(?:=|<|>|>=|<=|!=|(?:\s+(?:in|like|between)\s))",
                   re.I | re.A)
    for m in p.finditer(sql):
        col = m.group(1).lower()
        if not col or col.isdigit() or is_keyword_candidate(col):
            continue
        if col not in out:
            out.append(col)
    return set(out)


def is_fk_column(column: str | None, parent_table: str | None) -> bool:
    """列名是否为指向某张表的外键（user_id → perf_user、order_id → perf_order）。

    支持去掉表前缀（perf_）与复数形式。
    """
    if column is None or parent_table is None:
        return False
    col = column.lower()
    if not col.endswith("_id") and col != "id":
        return False
    return any(col == candidate + "_id" for candidate in name_variants(parent_table))


def is_id_column(column: str | None) -> bool:
    return (column or "").lower() == "id" if column is not None else False


def name_variants(table: str) -> list[str]:
    """表名的若干形态：原名、去前缀（perf_/t_/tb_）、去掉结尾复数 s。"""
    out: list[str] = []
    t = table.lower()
    _add_variant(out, t)
    for prefix in ("perf_", "t_", "tb_", "sys_", "biz_"):
        if t.startswith(prefix) and len(t) > len(prefix):
            _add_variant(out, t[len(prefix):])
    return out


def _add_variant(out: list[str], name: str | None) -> None:
    if not name or name in out:
        return
    out.append(name)
    if name.endswith("s") and len(name) > 1:
        singular = name[:-1]
        if singular not in out:
            out.append(singular)
