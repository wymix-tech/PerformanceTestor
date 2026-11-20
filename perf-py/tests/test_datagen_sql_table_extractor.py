"""SQL 表名与读写类型抽取等价单测：移植 SqlTableExtractorTest 全部 21 个用例。

重点验证「宁缺勿滥」的降级出口（子查询/CTE/多表 UPDATE warning）与
JOIN 关联边方向判定的三级证据优先级。
"""

from ptde.analyzer.lang_java._legacy_model import SqlAccess
from ptde.analyzer.lang_java.sql_table_extractor import (
    extract,
    extract_bare_columns,
    extract_columns_by_table,
    extract_join_edges,
    is_fk_column,
    sanitize,
)


def tables(sql: str | None) -> list[str]:
    return extract(sql).table_names()


def access_of(sql: str, table: str) -> SqlAccess | None:
    for t in extract(sql).tables:
        if t.table == table:
            return t.access
    return None


# ---------- 表名与读写类型 ----------

def test_select_from_single_table_is_read():
    sql = "select * from `order` o where o.status = 1"
    assert tables(sql) == ["order"]
    assert access_of(sql, "order") == SqlAccess.READ


def test_join_tables_are_all_read():
    sql = "select o.id, u.name from `order` o join user u on o.user_id = u.id where o.status = 1"
    assert tables(sql) == ["order", "user"]
    assert access_of("select o.id from `order` o join user u on o.user_id = u.id",
                     "user") == SqlAccess.READ


def test_insert_into_is_write():
    sql = "insert into `order` (user_id, amount) values (#{userId}, #{amount})"
    assert tables(sql) == ["order"]
    assert access_of(sql, "order") == SqlAccess.WRITE


def test_update_is_write():
    sql = "update `order` set status = 2 where id = #{id}"
    assert tables(sql) == ["order"]
    assert access_of(sql, "order") == SqlAccess.WRITE


def test_delete_from_is_write_not_read():
    sql = "delete from `order` where id = #{id}"
    assert tables(sql) == ["order"]
    assert access_of(sql, "order") == SqlAccess.WRITE


def test_schema_prefixed_table_name_is_trimmed():
    assert tables("select * from shop_db.`order` o") == ["order"]


# ---------- 降级出口 ----------

def test_subquery_produces_warning_instead_of_silence():
    r = extract("select * from (select id from `order`) t")
    assert r.warning is not None, "子查询必须给出降级提示，不能静默"
    assert "子查询" in r.warning


def test_cte_produces_warning():
    r = extract("with recent as (select id from `order`) select * from recent")
    assert r.warning is not None
    assert "CTE" in r.warning


def test_multi_table_update_produces_warning():
    r = extract("update `order` o, user u set o.status = 1")
    assert r.warning is not None
    assert "多表 UPDATE" in r.warning


def test_write_statement_without_target_warns_instead_of_guessing():
    r = extract("update set status = 1 where id = 1")
    assert r.warning is not None, "写语句未识别到目标表时必须告警，不得猜表"


# ---------- 防御性行为 ----------

def test_keywords_are_never_treated_as_table():
    r = extract("select * from `order` where status = 1 order by id desc limit 20")
    assert r.table_names() == ["order"]


def test_page_size_is_extracted_from_limit_literal():
    assert extract("select * from `order` limit 20").page_size == 20
    assert extract("select * from `order` limit #{size}").page_size == 0


def test_blank_sql_returns_empty():
    assert extract(None).tables == []
    assert extract("   ").tables == []


def test_invalid_tokens_are_rejected():
    assert sanitize("1abc") is None
    assert sanitize("`") is None
    assert sanitize("select") is None
    assert sanitize("`shop`.`order`") == "order"


def test_never_throws_on_broken_sql():
    r = extract("select from ((( where join ???")
    assert r.tables is not None


# ---------- JOIN 关联边 ----------

def test_join_on_edge_direction_from_fk_naming():
    # o.user_id = u.id → perf_order.user_id 指向 perf_user.id
    edges = extract_join_edges(
        "select o.id, u.name from perf_order o join perf_user u on o.user_id = u.id")
    assert len(edges) == 1
    e = edges[0]
    assert e.child_table == "perf_order"
    assert e.child_column == "user_id"
    assert e.parent_table == "perf_user"
    assert e.parent_column == "id"
    assert e.confidence == 0.9, "外键命名是最强证据"


def test_join_on_edge_direction_from_pk_side():
    # 列名无 FK 线索时，靠 "= u.id" 的主键侧判定方向
    edges = extract_join_edges(
        "select * from perf_order o join perf_user u on o.owner = u.id")
    assert len(edges) == 1
    assert edges[0].child_table == "perf_order"
    assert edges[0].parent_table == "perf_user"
    assert edges[0].confidence == 0.7


def test_reversed_join_is_handled():
    # join 子表、from 父表的写法，方向应反过来
    edges = extract_join_edges(
        "select * from perf_user u join perf_order o on o.user_id = u.id")
    assert len(edges) == 1
    assert edges[0].child_table == "perf_order", "子表应是外键所在的表"
    assert edges[0].parent_table == "perf_user"


def test_join_without_alias_is_supported():
    edges = extract_join_edges(
        "select * from perf_order join perf_user on perf_order.user_id = perf_user.id")
    assert len(edges) == 1
    assert edges[0].child_table == "perf_order"


def test_no_join_on_yields_no_edges():
    assert extract_join_edges("select * from perf_order where status = 1") == []
    assert extract_join_edges(None) == []


# ---------- 列归属与外键命名 ----------

def test_qualified_columns_are_mapped_by_table():
    cols = extract_columns_by_table(
        "select o.id, o.status from perf_order o where o.user_id = 1 and o.amount > 10")
    assert "perf_order" in cols
    assert "user_id" in cols["perf_order"]
    assert "status" in cols["perf_order"]


def test_bare_columns_collected_for_single_table_sql():
    cols = extract_columns_by_table("select * from `order` where status = 1 and amount > 10")
    assert "status" in cols.get("order", set())
    assert "amount" in cols.get("order", set())


def test_fk_naming_matches_table_variants():
    assert is_fk_column("user_id", "perf_user"), "去前缀后应能匹配"
    assert is_fk_column("order_id", "perf_order")
    assert is_fk_column("user_id", "users"), "复数表名应能匹配"
    assert not is_fk_column("status", "perf_user")
    assert not is_fk_column("user_id", "perf_order")


def test_broken_join_sql_does_not_throw():
    assert extract_join_edges("select from join on ???") == []
    assert extract_columns_by_table("select from ???") == {}


def test_extract_bare_columns_excludes_keywords_and_numbers():
    cols = extract_bare_columns("select * from t where status = 1 and name in ('a')")
    assert "status" in cols
    assert "1" not in cols
    assert "name" in cols
    # 对齐 Java 既有行为：反引号前缀在 lookbehind 中被排除，`name` 不命中
    cols2 = extract_bare_columns("select * from t where `name` in ('a')")
    assert "name" not in cols2
