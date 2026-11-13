"""WHERE 谓词抽取等价单测：移植 SqlPredicateExtractorTest 全部 13 个用例。

覆盖等值（含同列合并）/ IN / 范围（含 BETWEEN）/ 别名归属 / 占位符不命中。
"""

from datagen.model import Op
from datagen.sql_predicate_extractor import extract, has_any_alias


def preds(sql: str | None, table: str | None) -> list:
    return extract(sql, table)


def first(sql: str, table: str):
    ps = preds(sql, table)
    assert ps, f"期望抽出谓词: {sql}"
    return ps[0]


def test_equality_with_numeric_literal():
    sql = "select * from `order` o where o.status = 1"
    p = first(sql, "order")
    assert p.column == "status"
    assert p.op == Op.EQ
    assert p.values == [1]


def test_equality_with_string_literal():
    sql = "select * from `order` o where o.status = 'PAID'"
    p = first(sql, "order")
    assert p.op == Op.EQ
    assert p.values == ["PAID"]


def test_multiple_equality_on_same_column_merges_into_value_set():
    sql = "select * from `order` o where o.status = 1 or o.status = 2"
    p = first(sql, "order")
    assert p.op == Op.EQ
    assert p.values == [1, 2], "同列多等值应合并为值集"


def test_in_list_is_extracted():
    sql = "select * from `order` o where o.status in (1, 2, 3)"
    p = first(sql, "order")
    assert p.op == Op.IN
    assert p.values == [1, 2, 3]


def test_in_list_with_strings_is_extracted():
    sql = "select * from `order` o where o.status in ('NEW', 'PAID')"
    p = first(sql, "order")
    assert p.values == ["NEW", "PAID"]


def test_range_greater_than_sets_min():
    sql = "select * from `order` o where o.amount > 1000"
    p = first(sql, "order")
    assert p.op == Op.RANGE
    assert p.min == 1000.0


def test_range_less_than_sets_max():
    sql = "select * from `order` o where o.amount <= 50"
    p = first(sql, "order")
    assert p.op == Op.RANGE
    assert p.max == 50.0


def test_between_sets_both_bounds():
    sql = "select * from `order` o where o.amount between 100 and 5000"
    p = first(sql, "order")
    assert p.op == Op.RANGE
    assert p.min == 100.0
    assert p.max == 5000.0


def test_predicates_of_other_alias_are_not_attributed_to_target_table():
    sql = "select * from `order` o join user u on o.user_id = u.id where u.type = 9"
    assert preds(sql, "order") == [], "别的表/别名的谓词不能算到本表头上"
    assert first(sql, "user").column == "type"


def test_bare_predicates_are_accepted_when_no_alias_exists():
    sql = "select * from `order` where status = 1"
    p = first(sql, "order")
    assert p.column == "status"
    assert p.values == [1]


def test_placeholder_predicates_yield_nothing():
    sql = "select * from `order` o where o.status = #{status}"
    assert preds(sql, "order") == [], "占位符谓词无数据构造价值，应为空"


def test_blank_input_yields_nothing():
    assert preds(None, "order") == []
    assert preds("select * from `order`", None) == []


def test_join_on_condition_is_not_mistaken_for_predicate():
    sql = "select * from `order` o join user u on o.user_id = u.id"
    assert preds(sql, "order") == [], "JOIN ON 不是 WHERE 谓词"


def test_has_any_alias():
    assert has_any_alias("select * from `order` o where o.status = 1")
    assert not has_any_alias("select * from `order` where status = 1")
    # 对齐 Java 既有行为：order 刻意不入关键字黑名单（SqlTableExtractor 注释：
    # 表名 order 只可能出现在 from/join 之后，此时它是表名而非 ORDER BY），
    # 故 "from `order` order by" 中 group2=order 被视为别名——两侧行为一致
    assert has_any_alias("select * from `order` order by id")
    # 别名位是真正的关键字（group by）→ 不算别名
    assert not has_any_alias("select * from perf_order group by id")
