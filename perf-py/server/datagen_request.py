"""DataGenRequest 解析（阶段 5 · 步骤 7）：camelCase 请求体 → 内部 dict（Jackson 语义）。

对齐 com.perfserver.datagen.DataGenRequest 反序列化行为：
- 字段缺省 → Java 字段初始化默认值（rowCount=1000）；
- 显式 null 整数 → Jackson 基本类型收敛 0（rowCount=0 → 校验拒绝）；
- 未知字段忽略；connId/repoId/orphanRate/dryRun 等引用类型 null 透传 null。
"""

from __future__ import annotations


def _opt_int(payload: dict, key: str, default: int) -> int:
    """缺省 → Java 字段默认；显式 null → 0（Jackson 基本类型 null 收敛语义）。"""
    v = payload.get(key)
    if v is None:
        return 0 if key in payload else default
    try:
        return int(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"字段 {key} 必须是整数: {v!r}") from exc


def _opt_float(payload: dict, key: str) -> float | None:
    v = payload.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"字段 {key} 必须是数值: {v!r}") from exc


def _opt_str(payload: dict, key: str) -> str | None:
    v = payload.get(key)
    return None if v is None else str(v)


def _opt_bool(payload: dict, key: str) -> bool:
    v = payload.get(key)
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return bool(v)


def parse_datagen_request(payload: dict) -> dict:
    """camelCase 请求体 → 规范化 dict（tables 元素补齐 rowCount 默认与类型）。"""
    tables = None
    raw_tables = payload.get("tables")
    if isinstance(raw_tables, list) and raw_tables:
        tables = []
        for t in raw_tables:
            if not isinstance(t, dict):
                continue
            tables.append({
                "tableName": _opt_str(t, "tableName"),
                "rowCount": _opt_int(t, "rowCount", 1000),
                "fields": _opt_str(t, "fields"),
                "orphanRate": _opt_float(t, "orphanRate"),
            })
    return {
        "taskName": _opt_str(payload, "taskName"),
        "connId": payload.get("connId"),
        "dbUrl": _opt_str(payload, "dbUrl"),
        "dbUser": _opt_str(payload, "dbUser"),
        "dbPassword": _opt_str(payload, "dbPassword"),
        "tableName": _opt_str(payload, "tableName"),
        "rowCount": _opt_int(payload, "rowCount", 1000),
        "ruleJson": _opt_str(payload, "ruleJson"),
        "tables": tables,
        "orphanRate": _opt_float(payload, "orphanRate"),
        "dryRun": _opt_bool(payload, "dryRun"),
        "repoId": payload.get("repoId"),
    }
