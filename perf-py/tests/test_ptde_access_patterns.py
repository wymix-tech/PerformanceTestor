"""T1.4/T1.5 测试：AccessPattern 结构化提取 + 枚举四级发现分级证据。"""

from __future__ import annotations

from types import SimpleNamespace

from ptde.analyzer.access_patterns import (
    AccessPatternExtractor,
    extract_magic_hints,
)
from ptde.analyzer.enum_finder import EnumFinder
from ptde.models.access import PredicateOp, SqlKind

SQL = """
SELECT o.*, u.name FROM orders o
JOIN users u ON o.user_id = u.id
WHERE o.status = 'PAID' AND o.user_id = 7 AND o.created_at BETWEEN 1700000000 AND 1799999999
ORDER BY o.created_at DESC
LIMIT 20 OFFSET 40
"""


def test_full_access_pattern() -> None:
    ap = AccessPatternExtractor().extract(SQL, pattern_id="ap-1", source_ref="OrderMapper.xml")
    assert ap.tables == ["orders", "users"]
    assert ap.sql_kind == SqlKind.READ
    assert ap.joins and ap.joins[0].child_column == "user_id"
    assert ap.joins[0].parent_table == "users"
    assert ap.joins[0].parent_column == "id"
    # 谓词：EQ × 2 + RANGE（BETWEEN）→ range_fields 含 created_at
    ops = {f.column: f.op for f in ap.filters}
    assert ops["status"] == PredicateOp.EQ
    assert ops["user_id"] == PredicateOp.EQ
    assert ops["created_at"] == PredicateOp.RANGE
    assert "created_at" in ap.range_fields
    # 时间窗口（RANGE 谓词 + 时间字段名）
    assert ap.time_window and "from" in ap.time_window
    # ORDER BY / LIMIT / OFFSET（本层新增轻量解析）
    assert ap.order_by == ["o.created_at DESC"]
    assert ap.limit_offset.limit == 20
    assert ap.limit_offset.offset == 40
    assert not ap.unresolved
    assert ap.source_refs == ["OrderMapper.xml"]


def test_write_sql_kind() -> None:
    sql = "INSERT INTO orders (user_id, amount) VALUES (1, 9.9)"
    ap = AccessPatternExtractor().extract(sql, pattern_id="ap-w", sql_kind="WRITE")
    assert ap.sql_kind == SqlKind.WRITE
    assert ap.tables == ["orders"]


def test_group_by_extracted() -> None:
    sql = "SELECT status, COUNT(*) FROM orders GROUP BY status"
    ap = AccessPatternExtractor().extract(sql, pattern_id="ap-g")
    assert ap.group_by == ["status"]


def test_mysql_limit_offset_syntax() -> None:
    ap = AccessPatternExtractor().extract(
        "SELECT * FROM orders LIMIT 40, 20", pattern_id="ap-l"
    )
    assert ap.limit_offset.limit == 20
    assert ap.limit_offset.offset == 40


def test_unparsed_sql_registered_unknown_not_dropped() -> None:
    """A3：来源 SQL 无法解析 → unresolved 登记（UNKNOWN）而非丢弃。"""
    ap = AccessPatternExtractor().extract("", pattern_id="ap-e")
    assert ap.unresolved and ap.unresolved_reason == "empty-sql"
    ap2 = AccessPatternExtractor().extract("@@not sql at all ###", pattern_id="ap-x")
    assert ap2.unresolved
    assert ap2.unresolved_reason
    # unresolved 也保留结构化模型（可导出、可统计）
    assert ap2.id == "ap-x"


def test_exception_path_registered() -> None:
    ap = AccessPatternExtractor().extract(None, pattern_id="ap-n")
    assert ap.unresolved and ap.unresolved_reason == "empty-sql"


def test_magic_hints_extraction() -> None:
    # legacy extract_literals 仅匹配表限定列（alias.column），语义保持
    hints = extract_magic_hints(
        "SELECT o.* FROM orders o WHERE o.status = 'PAID' AND o.channel IN (1, 2)",
        "orders", "OrderMapper.xml",
    )
    by_col = {h["column"]: h for h in hints}
    assert by_col["status"]["values"] == ["PAID"]
    assert by_col["channel"]["values"] == [1, 2]
    assert by_col["status"]["source_refs"] == ["OrderMapper.xml"]


# ---------- T1.5 枚举四级发现 ----------

def _col(name, type_name=None, comment=None):
    return SimpleNamespace(name=name, type_name=type_name, comment=comment)


class _FakeDialect:
    @staticmethod
    def get_enum_values(conn, table, column):
        return ["PENDING", "PAID"]


def test_level1_dialect_enum_confirmed() -> None:
    findings = EnumFinder().find(
        "orders", _col("status", "varchar(16)"),
        conn=object(), dialect=_FakeDialect(),
    )
    assert len(findings) == 1
    assert findings[0].status.value == "CONFIRMED"
    assert findings[0].value["values"] == ["PENDING", "PAID"]
    assert findings[0].value["enum_source"] == "dialect_enum"
    assert findings[0].database_object == "orders.status"


def test_level2_inline_enum_confirmed() -> None:
    findings = EnumFinder().find(
        "orders", _col("status", "ENUM('NEW','PAID')")
    )
    assert findings[0].status.value == "CONFIRMED"
    assert findings[0].value["values"] == ["NEW", "PAID"]


def test_level3_check_clause_confirmed() -> None:
    clause = "((`status` in ('NEW','PAID')))"
    findings = EnumFinder().find(
        "orders", _col("status", "varchar(16)"), check_clause=clause
    )
    assert findings[0].status.value == "CONFIRMED"
    assert findings[0].value["values"] == ["NEW", "PAID"]


def test_level3_comment_inferred() -> None:
    findings = EnumFinder().find(
        "orders", _col("status", "varchar(16)", comment="状态 0=待支付 1=已支付")
    )
    assert findings[0].status.value == "INFERRED"
    assert findings[0].confidence == 0.6


def test_level4_java_enum_inferred() -> None:
    findings = EnumFinder().find(
        "orders", _col("order_status", "varchar(16)"),
        java_enums={"OrderStatus": ["NEW", "PAID"]},
    )
    assert findings[0].status.value == "INFERRED"
    assert findings[0].value["values"] == ["NEW", "PAID"]


def test_four_sources_independent_evidence_entries() -> None:
    """F6：各来源独立证据条目（方言内 CHECK 与独立解析两条路径均登记）。"""
    findings = EnumFinder().find(
        "orders",
        _col("status", "ENUM('A','B')", comment="状态 0=待支付 1=已支付"),
        check_clause="((status in ('X','Y')))",
    )
    sources = [f.value["enum_source"] for f in findings]
    # 内联 ENUM 与 CHECK 与注释三条独立证据（方言路径无 conn 不触发）
    assert "inline_enum" in sources and "check_constraint" in sources
    assert "column_comment" in sources


def test_no_hit_suspect_column_unknown() -> None:
    findings = EnumFinder().find("orders", _col("order_type", "varchar(16)"))
    assert len(findings) == 1
    assert findings[0].status.value == "UNKNOWN"
    assert findings[0].value["values"] == []


def test_no_hit_non_suspect_silent() -> None:
    assert EnumFinder().find("orders", _col("amount", "decimal(18,2)")) == []
