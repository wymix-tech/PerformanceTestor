"""枚举值四级发现等价单测：移植 SemanticParserTest 的枚举 4 个用例 + 覆盖补强。"""

from ptde.models.wire import ColumnMeta
from ptde_common.enum_value_discoverer import (
    discover,
    match_java_enum,
    parse_check_clause,
    parse_comment,
    parse_inline_enum,
    suspect_enum_column,
)


def col(name: str, type_name: str, comment: str | None = None) -> ColumnMeta:
    return ColumnMeta(name=name, type_name=type_name, size=64, nullable=True, comment=comment)


# ---------- SemanticParserTest 移植 ----------

def test_inline_enum_parsed():
    assert parse_inline_enum("ENUM('NEW','PAID','CANCEL')") == ["NEW", "PAID", "CANCEL"]
    assert parse_inline_enum("VARCHAR(16)") == []


def test_check_clause_parsed():
    assert parse_check_clause("(`status` in ('a','b'))") == ["a", "b"]
    assert parse_check_clause("status IN (1,2,3)") == [1, 2, 3]


def test_comment_pairs_parsed():
    assert parse_comment("状态 1=待支付 2=已支付 3=已取消",
                         col("status", "TINYINT")) == [1, 2, 3]
    assert parse_comment("普通注释", col("remark", "VARCHAR")) == []


def test_java_enum_matched_by_name():
    assert match_java_enum("order_status", {"OrderStatus": ["NEW", "PAID"]}) == ["NEW", "PAID"]
    assert match_java_enum("remark", {"OrderStatus": ["NEW"]}) == []


# ---------- discover 编排与覆盖补强 ----------

def test_discover_priority_inline_enum_beats_comment():
    c = col("status", "ENUM('A','B')", comment="1=x 2=y")
    assert discover(c) == ["A", "B"]


def test_discover_comment_beats_java_enum():
    c = col("order_status", "TINYINT", comment="1=x 2=y")
    assert discover(c, java_enums={"OrderStatus": ["NEW"]}) == [1, 2]


def test_discover_check_clause_used_when_no_inline():
    c = col("status", "VARCHAR(16)")
    assert discover(c, check_clause="(`status` in ('NEW','PAID'))") == ["NEW", "PAID"]


def test_discover_falls_back_to_java_enum():
    c = col("order_status", "TINYINT")
    assert discover(c, java_enums={"OrderStatus": ["NEW", "PAID"]}) == ["NEW", "PAID"]


def test_discover_empty_when_nothing_hits():
    assert discover(col("remark", "VARCHAR(64)")) == []


def test_suspect_enum_column():
    assert suspect_enum_column(col("order_status", "VARCHAR"))
    assert suspect_enum_column(col("type", "VARCHAR"))
    assert suspect_enum_column(col("biz_category", "VARCHAR"))
    assert not suspect_enum_column(col("amount", "DECIMAL(12,2)"))


def test_comment_needs_at_least_two_pairs():
    # 仅 1 对键值不视为枚举注释（避免误报）
    assert parse_comment("数量 1=件", col("qty", "TINYINT")) == []
