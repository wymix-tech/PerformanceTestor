"""MySQL 慢查询日志解析器：逐行对齐 MysqlSlowLogParser.java。

解析标准 MySQL 慢日志格式：

    # Time: 2026-08-27T13:00:00.123456Z
    # User@Host: root[root] @ localhost [127.0.0.1]
    # Query_time: 1.283376  Lock_time: 0.000145  Rows_sent: 10  Rows_examined: 1000
    SELECT * FROM t_user WHERE id = 1;

语义边界（与 Java 一致）：
- 条目以 "# Time:" 行开始；SQL 语句行（可多行）持续累积到下一个 "# Time:"
- query_time_ms <= 0 或 SQL 为空的条目丢弃
- 其他注释行（"# admin@" 等）忽略
- 完整 Query_time 行解析失败时退化为短格式（仅 Query_time）
- 时间解析失败兜底为当前时间（Java LocalDateTime.now 同款行为）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

TIME_PATTERN = re.compile(r"^# Time: (.*)$")
USER_HOST_PATTERN = re.compile(r"^# User@Host: ([^\[]+)\[[^\]]*\] @ ([^\[]+)\s*\[?([^\]]*)\]?")
QUERY_TIME_PATTERN = re.compile(
    r"^# Query_time: ([\d.]+)\s+Lock_time: ([\d.]+)\s+Rows_sent: (\d+)\s+Rows_examined: (\d+)"
)
QUERY_TIME_SHORT_PATTERN = re.compile(r"^# Query_time: ([\d.]+)")


@dataclass
class Entry:
    execute_time: datetime | None = None
    user: str = ""
    host: str = ""
    query_time_ms: float = 0.0
    lock_time_ms: float = 0.0
    rows_sent: int = 0
    rows_examined: int = 0
    sql_text: str = field(default="")


def _parse_time(raw: str) -> datetime:
    """解析 MySQL 时间格式（支持带 T 的 ISO 与带空格的本地格式）。"""
    v = raw.strip()
    dot = v.find(".")
    if dot > 0:
        v = v[:dot]
    v = v.replace("T", " ").strip()
    try:
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        # 兜底（对齐 Java：解析失败返回 now）
        return datetime.now()


def _parse_ms(raw: str) -> float:
    try:
        return float(raw.strip()) * 1000.0
    except ValueError:
        return 0.0


def _finish(entries: list[Entry], entry: Entry | None, sql: str) -> None:
    if entry is None or entry.query_time_ms <= 0:
        return
    sql_text = sql.strip()
    if not sql_text:
        return
    entry.sql_text = sql_text
    entries.append(entry)


def parse(content: str | None) -> list[Entry]:
    """解析日志文本，返回慢 SQL 条目列表。"""
    entries: list[Entry] = []
    if not content or not content.strip():
        return entries
    lines = content.splitlines()
    current: Entry | None = None
    sql = ""

    for line in lines:
        if line.startswith("# Time:"):
            _finish(entries, current, sql)
            current = Entry()
            sql = ""
            m = TIME_PATTERN.match(line)
            if m:
                current.execute_time = _parse_time(m.group(1).strip())
        elif line.startswith("# User@Host:") and current is not None:
            m = USER_HOST_PATTERN.match(line)
            if m:
                current.user = m.group(1).strip()
                current.host = m.group(2).strip()
        elif line.startswith("# Query_time:") and current is not None:
            m = QUERY_TIME_PATTERN.match(line)
            if m:
                current.query_time_ms = _parse_ms(m.group(1))
                current.lock_time_ms = _parse_ms(m.group(2))
                current.rows_sent = int(m.group(3))
                current.rows_examined = int(m.group(4))
            else:
                short = QUERY_TIME_SHORT_PATTERN.match(line)
                if short:
                    current.query_time_ms = _parse_ms(short.group(1))
        elif line.startswith("#"):
            # 其他注释行（# admin@ 等）忽略
            pass
        elif line.strip() and current is not None:
            # SQL 语句行（可能多行，直到下一个 # Time）
            sql = f"{sql} {line.strip()}" if sql else line.strip()

    _finish(entries, current, sql)
    return entries
