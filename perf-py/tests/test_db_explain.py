"""数据库只读检测单元测试（阶段 5 · 步骤 10）：逐语义对齐 Java DbExplainService。

契约：validate 安全校验文案逐字、sanitizeSql 前缀噪音剥离、extractTables 去重上限 5、
urlHost、formatSummary 分段文本。
"""

from server.db_explain import (
    DbExplainService,
    extract_tables,
    format_summary,
    sanitize_sql,
    validate,
)

# ------------------------------------------------------------ 安全校验

def test_validate_verbatim_messages():
    assert validate(None, "SELECT 1") == "未配置数据库连接"
    assert validate("jdbc:postgresql://h/db", "SELECT 1") == \
        "JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:mariadb://"
    assert validate("jdbc:mysql://h/db", "") == "SQL 为空"
    assert validate("jdbc:mysql://h/db", "UPDATE t SET a=1") == \
        "仅支持对 SELECT/WITH 查询执行 EXPLAIN"
    assert validate("jdbc:mysql://h/db", "SELECT 1; DROP TABLE t") == \
        "SQL 含分号或注释，疑似多语句，已拒绝"
    assert validate("jdbc:mysql://h/db", "SELECT 1 -- x") == \
        "SQL 含分号或注释，疑似多语句，已拒绝"
    # 非 SELECT/WITH 开头先命中"仅支持 SELECT/WITH"分支（Java 校验顺序如此）
    assert validate("jdbc:mysql://h/db", "EXPLAIN ANALYZE SELECT 1") == \
        "仅支持对 SELECT/WITH 查询执行 EXPLAIN"
    assert validate("jdbc:mysql://h/db", "SELECT * INTO outfile '/x' FROM t") == \
        "SQL 含受限关键字"
    assert validate("jdbc:mysql://h/db", "SELECT " + "1" * 4001) == "SQL 过长"
    assert validate("jdbc:mysql://h/db", "SELECT 1") is None
    assert validate("jdbc:mysql://h/db", "WITH c AS (SELECT 1) SELECT * FROM c") is None


def test_sanitize_sql_strips_driver_noise():
    assert sanitize_sql("org.mysql.ClientPreparedStatement: SELECT * FROM t") == \
        "SELECT * FROM t"
    assert sanitize_sql("  select 1") == "select 1"


def test_extract_tables_dedup_and_limit():
    sql = "SELECT * FROM t_a JOIN t_b LEFT JOIN t_c , t_a JOIN t_d JOIN t_e JOIN t_f"
    assert extract_tables(sql) == ["t_a", "t_b", "t_c", "t_d", "t_e"]  # 最多 5 张 + 去重


def test_url_host():
    svc = DbExplainService()
    assert svc.url_host("jdbc:mysql://10.0.0.20:3306/perf?useSSL=false") == "10.0.0.20:3306/perf"
    assert svc.url_host("jdbc:mysql://10.0.0.20:3306/perf") == "10.0.0.20:3306/perf"
    assert svc.url_host("not-a-url") == ""
    assert svc.url_host(None) == ""


def test_format_summary_segments():
    assert format_summary(None) == ""
    assert format_summary({}) == ""
    assert format_summary({"error": "boom"}) == "（数据库检测不可用：boom）"
    summary = format_summary({
        "explainRows": [{"id": "1", "type": "ALL"}],
        "indexes": {"t": "PRIMARY(id) [UNIQUE]"},
        "tableRows": {"t": 998},
    })
    assert "EXPLAIN 执行计划：" in summary
    assert "现有索引：" in summary and "t: PRIMARY(id) [UNIQUE]" in summary
    assert "表规模（估算行数）：t=998" in summary


def test_analyze_sql_rejects_before_connect():
    """校验失败不发起连接（connect_fn 计数）。"""
    calls = []

    def _conn(*_a):
        calls.append(1)
        raise AssertionError("不应连接")

    out = DbExplainService(_conn).analyze_sql("jdbc:postgresql://h/db", "u", "p", "SELECT 1")
    assert out == {"error": "JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:mariadb://"}
    assert not calls
