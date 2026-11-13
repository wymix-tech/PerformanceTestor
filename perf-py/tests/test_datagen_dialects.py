"""方言纯逻辑等价单测：移植 MySQLDialectTest 与 PostgreSQLDialectH2Test 的
非连接部分（URL 路由/增强、标识符引用、INSERT 构建、CHECK 解析、SERIAL 判定）。
"""

from datagen.dialects.mysql import MysqlDialect
from datagen.dialects.postgres import PostgresDialect, is_serial_default, parse_check_definition


def test_mysql_supports_only_mysql_urls():
    d = MysqlDialect()
    assert d.supports("jdbc:mysql://localhost:3306/demo")
    assert not d.supports("jdbc:postgresql://localhost:5432/demo")
    assert not d.supports(None)


def test_mysql_enrich_jdbc_url():
    d = MysqlDialect()
    base = "jdbc:mysql://localhost:3306/demo"
    assert d.enrich_jdbc_url(base) == (base
                                       + "?rewriteBatchedStatements=true&useInformationSchema=true")
    with_qs = base + "?useSSL=false"
    assert d.enrich_jdbc_url(with_qs) == (with_qs
                                          + "&rewriteBatchedStatements=true&useInformationSchema=true")
    already = base + "?rewriteBatchedStatements=true"
    assert d.enrich_jdbc_url(already) == already, "参数已存在时原样返回，不重复追加"


def test_mysql_quote_identifier():
    d = MysqlDialect()
    assert d.quote_identifier("perf_order") == "`perf_order`"
    assert d.quote_identifier("we`ird") == "`we``ird`", "内嵌反引号转义"


def test_mysql_build_insert_sql():
    # Java 端占位符统一为 ?（JDBC 标准）；Python 各驱动 paramstyle 不同，
    # 占位符随方言 param 属性输出（pymysql=%s；测试方言覆盖为 ?）
    d = MysqlDialect()
    assert d.build_insert_sql("perf_order", ["user_id", "amount"]) == \
        "INSERT INTO `perf_order` (`user_id`, `amount`) VALUES (%s, %s)"


def test_pg_supports_only_postgres_urls():
    d = PostgresDialect()
    assert d.supports("jdbc:postgresql://localhost:5432/demo")
    assert not d.supports("jdbc:mysql://localhost:3306/demo")
    assert not d.supports(None)


def test_pg_enrich_jdbc_url_appends_batched_inserts_and_string_type():
    d = PostgresDialect()
    base = "jdbc:postgresql://h/d"
    assert d.enrich_jdbc_url(base) == (base
                                       + "?reWriteBatchedInserts=true&stringtype=unspecified")
    with_qs = "jdbc:postgresql://h/d?sslmode=disable"
    assert d.enrich_jdbc_url(with_qs) == (with_qs
                                          + "&reWriteBatchedInserts=true&stringtype=unspecified")
    enriched = d.enrich_jdbc_url(with_qs)
    assert d.enrich_jdbc_url(enriched) == enriched, "两个参数均存在时原样返回，不重复追加"


def test_pg_enrich_jdbc_url_keeps_custom_string_type():
    d = PostgresDialect()
    with_st = "jdbc:postgresql://h/d?stringtype=varchar"
    assert d.enrich_jdbc_url(with_st) == with_st + "&reWriteBatchedInserts=true"


def test_pg_quote_identifier_uses_double_quotes():
    d = PostgresDialect()
    assert d.quote_identifier("perf_order") == '"perf_order"'
    assert d.quote_identifier('we"ird') == '"we""ird"', "内嵌双引号转义"
    assert d.quote_identifier(None) == '""'


def test_pg_build_insert_sql_quoted_and_placeholder():
    # 占位符随方言 param 属性（psycopg2=%s），对齐 Java ? 的"值一律占位符"语义
    d = PostgresDialect()
    assert d.build_insert_sql("perf_order", ["user_id", "amount"]) == \
        'INSERT INTO "perf_order" ("user_id", "amount") VALUES (%s, %s)'


def test_parse_check_definition_array_and_in_list():
    assert parse_check_definition(
        "CHECK ((status = ANY (ARRAY['PENDING'::character varying, "
        "'PAID'::character varying, 'SHIPPED'::character varying])))") == [
        "PENDING", "PAID", "SHIPPED"]
    assert parse_check_definition("CHECK ((code IN ('A', 'B')))") == ["A", "B"]
    assert parse_check_definition("CHECK ((level IN (1, 2, 3)))") == ["1", "2", "3"]
    assert parse_check_definition(None) == []
    assert parse_check_definition("CHECK ((age > 0))") == [], "非枚举约束不误报"


def test_serial_default_detected():
    assert is_serial_default("nextval('perf_user_id_seq'::regclass)")
    assert is_serial_default("NEXTVAL('s'::regclass)")
    assert not is_serial_default(None)
    assert not is_serial_default("0")
