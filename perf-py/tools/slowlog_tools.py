"""慢日志工具：MySQL 慢日志解析 + 聚合统计（移植自 slowlog/，golden 基线对齐）。"""

from __future__ import annotations

from pathlib import Path

from slowlog.mysql_parser import parse
from tools.registry import Tool

SLOWLOG_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "慢日志文件路径"},
        "content": {"type": "string", "description": "或直接给慢日志文本"},
        "top_n": {"type": "integer", "description": "Top N 慢 SQL 条数（默认 10）",
                  "default": 10},
    },
    "required": [],
}


def slowlog_analyze(args: dict) -> dict:
    """解析慢日志：Top N 慢 SQL + 汇总（条数/耗时分布/扫描行数）。"""
    path = args.get("path")
    content = args.get("content")
    if not content:
        if not path:
            return {"error": "需要 path 或 content 二选一"}
        p = Path(path)
        if not p.is_file():
            return {"error": f"文件不存在: {path}"}
        content = p.read_text(encoding="utf-8", errors="replace")
    entries = parse(content)
    if not entries:
        return {"total": 0, "top": [], "summary": "未解析到慢 SQL 条目"}

    top_n = int(args.get("top_n") or 10)
    ordered = sorted(entries, key=lambda e: e.query_time_ms, reverse=True)
    total_query_ms = sum(e.query_time_ms for e in entries)
    total_examined = sum(e.rows_examined for e in entries)
    return {
        "total": len(entries),
        "summary": {
            "maxQueryTimeMs": ordered[0].query_time_ms,
            "avgQueryTimeMs": round(total_query_ms / len(entries), 2),
            "totalQueryTimeMs": round(total_query_ms, 2),
            "totalRowsExamined": total_examined,
        },
        "top": [
            {
                "time": e.execute_time.isoformat(sep=" ") if e.execute_time else None,
                "user": e.user,
                "host": e.host,
                "queryTimeMs": e.query_time_ms,
                "lockTimeMs": e.lock_time_ms,
                "rowsSent": e.rows_sent,
                "rowsExamined": e.rows_examined,
                "sql": e.sql_text,
            }
            for e in ordered[:top_n]
        ],
    }


SLOWLOG_TOOLS = [
    Tool(
        name="slowlog_analyze",
        description="解析 MySQL 慢查询日志：返回 Top N 慢 SQL（含扫描行数）与"
                    "聚合统计（条数/最大/平均耗时/总扫描行数），用于关联压测 P95/P99 恶化根因。",
        parameters=SLOWLOG_SCHEMA,
        handler=slowlog_analyze,
    ),
]
