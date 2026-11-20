"""SQL 字面量/JOIN 关联容错抽取等价单测：移植 SqlLiteralExtractor 语义
+ SemanticParserTest 的 SQL 抽取 3 个用例。
"""

from ptde.analyzer.lang_java.sql_literal_extractor import extract_join_relations, extract_literals
from ptde.models.wire import MagicHint, RelationHint


def test_sql_literals_extracted():
    sql = "SELECT * FROM perf_order o WHERE o.status = 'SHIPPED' AND o.user_id = 7"
    hints = extract_literals(sql, "perf_order", "OrderMapper.java:20")
    assert any(h.column == "status" and "SHIPPED" in h.values for h in hints)


def test_sql_literals_numeric_values_are_int():
    sql = "SELECT * FROM perf_order o WHERE o.user_id = 7"
    hints = extract_literals(sql, "perf_order", "src:1")
    hints = [h for h in hints if h.column == "user_id"]
    assert len(hints) == 1
    assert hints[0].values == [7]
    assert hints[0].table == "perf_order"
    assert hints[0].kind == MagicHint.KIND_LITERAL
    assert hints[0].source_refs == ["src:1"]


def test_sql_literals_in_list_and_alias_binding():
    sql = "SELECT * FROM perf_order o WHERE o.status IN ('NEW', 'PAID')"
    hints = [h for h in extract_literals(sql, "perf_order", None) if h.column == "status"]
    assert hints[0].values == ["NEW", "PAID"]


def test_sql_literals_ignored_for_other_table():
    sql = "SELECT * FROM perf_user u WHERE u.type = 9"
    assert [h for h in extract_literals(sql, "perf_order", None)] == []


def test_sql_join_relations_extracted():
    sql = "SELECT o.* FROM perf_order o JOIN perf_user u ON o.user_id = u.id"
    hints = extract_join_relations(sql, {"perf_order", "perf_user"})
    assert len(hints) == 1
    assert hints[0].child_table == "perf_order"
    assert hints[0].child_column == "user_id"
    assert hints[0].parent_table == "perf_user"
    assert hints[0].parent_column == "id"
    assert hints[0].source == RelationHint.SRC_ORM_JOIN
    assert hints[0].confidence == 0.9


def test_malformed_sql_silently_skipped():
    broken = "SELECT !!! FROM ((( WHERE"
    assert extract_literals(broken, "t", "x") == []
    assert extract_join_relations(broken, {"t"}) == []


def test_blank_input():
    assert extract_literals(None, "t", "x") == []
    assert extract_literals("select 1", None, "x") == []
    assert extract_join_relations(None, {"t"}) == []
