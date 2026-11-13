"""MysqlSlowLogParser 移植测试 + golden 样本对比。

Java 侧该解析器无单测；本套件以 golden 样本文件建立基线：
真实 MySQL 慢日志片段（多行 SQL、SET TIMESTAMP、变体格式、脏数据）→
期望条目清单逐字段断言。
"""

from datetime import datetime
from pathlib import Path

from slowlog.mysql_parser import Entry, parse

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "slowlog_golden.log"

MULTILINE_SAMPLE = """\
# Time: 2026-08-27T13:00:00.123456Z
# User@Host: root[root] @ localhost [127.0.0.1]  Id:    11
# Query_time: 1.283376  Lock_time: 0.000145  Rows_sent: 10  Rows_examined: 1000
SELECT o.id, u.name
  FROM perf_order o
  JOIN perf_user u ON u.id = o.user_id
 WHERE o.status = 'SHIPPED';
# Time: 2026-08-27T13:00:05Z
# User@Host: perf[perf] @  [10.0.0.8]
# Query_time: 0.5  Lock_time: 0  Rows_sent: 1  Rows_examined: 50000
SELECT count(*) FROM perf_order WHERE status = 'SHIPPED';
"""


def test_parse_standard_format():
    entries = parse(MULTILINE_SAMPLE)
    assert len(entries) == 2

    e1 = entries[0]
    assert e1.execute_time == datetime(2026, 8, 27, 13, 0, 0)
    assert e1.user == "root"
    assert e1.host == "localhost"
    assert e1.query_time_ms == 1283.376
    assert e1.lock_time_ms == 0.145
    assert e1.rows_sent == 10
    assert e1.rows_examined == 1000
    # 多行 SQL 用单空格连接
    assert e1.sql_text == (
        "SELECT o.id, u.name FROM perf_order o JOIN perf_user u ON u.id = o.user_id WHERE o.status = 'SHIPPED';"
    )

    e2 = entries[1]
    assert e2.user == "perf"
    assert e2.host == ""  # "@  [ip]" 双空格 → host 空串（对齐 Java 正则行为）
    assert e2.query_time_ms == 500.0
    assert e2.rows_examined == 50000


def test_parse_query_time_short_variant():
    # 短格式：只有 Query_time（Lock_time 等缺失）
    content = "# Time: 2026-08-27T13:00:00Z\n# Query_time: 2.5\nSELECT 1;\n"
    entries = parse(content)
    assert len(entries) == 1
    assert entries[0].query_time_ms == 2500.0
    assert entries[0].lock_time_ms == 0.0
    assert entries[0].sql_text == "SELECT 1;"


def test_discards_entries_without_sql_or_zero_query_time():
    content = (
        "# Time: 2026-08-27T13:00:00Z\n"
        "# Query_time: 1.0  Lock_time: 0  Rows_sent: 0  Rows_examined: 10\n"  # 无 SQL → 丢弃
        "# Time: 2026-08-27T13:00:02Z\n"
        "# Query_time: 0  Lock_time: 0  Rows_sent: 0  Rows_examined: 10\n"
        "SELECT 1;\n"  # Query_time=0 → 丢弃
    )
    assert parse(content) == []


def test_ignores_other_comment_lines():
    content = (
        "# admin@localhost on emp\n"
        "# Time: 2026-08-27T13:00:00Z\n"
        "# Query_time: 1  Lock_time: 0  Rows_sent: 1  Rows_examined: 1\n"
        "SELECT 1;\n"
    )
    entries = parse(content)
    assert len(entries) == 1


def test_parse_blank_and_none():
    assert parse("") == []
    assert parse(None) == []
    assert parse("   \n  ") == []


def test_golden_file_sample():
    """golden 样本回归：真实慢日志片段 → 期望摘要。"""
    entries = parse(GOLDEN.read_text(encoding="utf-8"))
    # golden 基线：条目数 + 每条 (user, host, query_ms, rows_examined, SQL 首词)
    assert len(entries) == 3
    # 注意 host 为空串是 Java 正则 [^\[]+ 的真实行为（"@  [ip]" 双空格时
    # group2 贪吃空格、trim 后为空）——Python 侧保持逐位对齐而非"修正"
    summary = [
        (e.user, e.host, e.query_time_ms, e.rows_examined, e.sql_text.split(" ", 1)[0])
        for e in entries
    ]
    assert summary == [
        ("root", "localhost", 1283.376, 1000, "use"),
        ("perf", "", 500.0, 50000, "SET"),
        ("app", "", 2500.0, 50000, "SET"),
    ]


def test_entry_defaults():
    e = Entry()
    assert e.query_time_ms == 0.0
    assert e.sql_text == ""
