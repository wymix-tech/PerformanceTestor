"""观测查询路由：补齐 /api/apps、/api/jvm/metrics、/api/traces、/api/slow-sqls、
/api/slow-commands、/api/hotspots。契约对齐 perf-web/src/api/index.ts。

四张时序表均为游标分页（(time_col DESC, id DESC) 复合键），
统一包装 {"code": 0, "message": "success", "data": ...}。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Query, Request

from server.cursor import build_cursor_page, clamp_size, decode_cursor, parse_time
from server.profile_merge import DEFAULT_MAX_NODES, folded_to_tree, merge_folded

router = APIRouter(prefix="/api")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _fmt(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def _cursor(cursor: str | None):
    """解码游标：坏游标直接返回 Java 风格错误包装（对齐全局异常处理口径）。"""
    try:
        return decode_cursor(cursor), None
    except ValueError as exc:
        return None, {"code": 1, "message": f"服务器内部错误: {exc}", "data": None}


@router.get("/apps")
def list_apps(request: Request) -> dict:
    return _ok(request.app.state.collector_repo.list_apps())


@router.get("/jvm/metrics")
def jvm_metrics(
    request: Request,
    app: str | None = None,
    start: str | None = None,
    end: str | None = None,
    size: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict:
    page_size = clamp_size(size, limit)
    cursor_pair, err = _cursor(cursor)
    if err:
        return err
    rows = request.app.state.collector_repo.query_jvm_metrics(
        app=app, start=parse_time(start), end=parse_time(end),
        cursor_pair=cursor_pair, limit_plus_one=page_size + 1,
    )
    return _ok(build_cursor_page(
        rows, page_size,
        time_getter=lambda r: r.collect_time,
        id_getter=lambda r: r.id,
        serializer=lambda r: {
            "id": r.id, "appName": r.app_name, "collectTime": _fmt(r.collect_time),
            "heapUsedMb": r.heap_used_mb, "heapMaxMb": r.heap_max_mb,
            "nonHeapUsedMb": r.non_heap_used_mb,
            "youngGcCount": r.young_gc_count, "youngGcMs": r.young_gc_ms,
            "fullGcCount": r.full_gc_count, "fullGcMs": r.full_gc_ms,
            "threadCount": r.thread_count, "cpuUsage": r.cpu_usage,
            "systemCpu": r.system_cpu,
            # P1-5 扩展指标（可选 JSON 列，旧探针为 None）
            "extraMetrics": (json.loads(r.extra_metrics)
                             if getattr(r, "extra_metrics", None) else None),
        },
    ))


@router.get("/traces")
def list_traces(
    request: Request,
    app: str | None = None,
    method: str | None = None,
    minCostMs: float | None = Query(default=None),
    stressTaskId: int | None = Query(default=None),
    start: str | None = None,
    end: str | None = None,
    size: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict:
    page_size = clamp_size(size, limit)
    cursor_pair, err = _cursor(cursor)
    if err:
        return err
    rows = request.app.state.collector_repo.query_traces(
        app=app, method=method, min_cost_ms=minCostMs,
        start=parse_time(start), end=parse_time(end),
        cursor_pair=cursor_pair, limit_plus_one=page_size + 1,
        stress_task_id=stressTaskId,
    )
    return _ok(build_cursor_page(
        rows, page_size,
        time_getter=lambda r: r.start_time,
        id_getter=lambda r: r.id,
        serializer=lambda r: {
            "id": r.id, "traceId": r.trace_id, "appName": r.app_name,
            "spanId": r.span_id, "parentSpanId": r.parent_span_id,
            "rootMethod": r.root_method, "totalMs": r.total_ms,
            "startTime": _fmt(r.start_time), "stressTaskId": r.stress_task_id,
        },
    ))


@router.get("/traces/{trace_id}")
def trace_detail(trace_id: str, request: Request) -> dict:
    data = request.app.state.collector_repo.get_trace_with_child_spans(trace_id)
    if data is None:
        return {"code": 1, "message": f"trace not found: {trace_id}", "data": None}
    row = data["main"]
    return _ok({
        "id": row.id, "traceId": row.trace_id, "appName": row.app_name,
        "spanId": row.span_id, "parentSpanId": row.parent_span_id,
        "rootMethod": row.root_method, "traceJson": row.trace_json,
        "totalMs": row.total_ms, "startTime": _fmt(row.start_time),
        # P2-2 微服务串联：主链路之外的关联服务 span 列表（单服务数据为空，零破坏）
        "childSpans": data["childSpans"],
    })


@router.get("/slow-sqls")
def list_slow_sqls(
    request: Request,
    app: str | None = None,
    minCostMs: float | None = Query(default=None),
    stressTaskId: int | None = Query(default=None),
    start: str | None = None,
    end: str | None = None,
    size: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict:
    page_size = clamp_size(size, limit)
    cursor_pair, err = _cursor(cursor)
    if err:
        return err
    rows = request.app.state.collector_repo.query_slow_sqls(
        app=app, min_cost_ms=minCostMs, start=parse_time(start), end=parse_time(end),
        cursor_pair=cursor_pair, limit_plus_one=page_size + 1,
        stress_task_id=stressTaskId,
    )
    return _ok(build_cursor_page(
        rows, page_size,
        time_getter=lambda r: r.execute_time,
        id_getter=lambda r: r.id,
        serializer=lambda r: {
            "id": r.id, "appName": r.app_name, "sqlText": r.sql_text,
            "costMs": r.cost_ms, "source": r.source, "dbHost": r.db_host,
            "methodChain": r.method_chain, "executeTime": _fmt(r.execute_time),
            "sqlFingerprint": r.sql_fingerprint,
            "stressTaskId": r.stress_task_id,
        },
    ))


@router.get("/slow-sqls/aggregate")
def aggregate_slow_sqls(
    request: Request,
    app: str | None = None,
    stressTaskId: int | None = Query(default=None),
    start: str | None = None,
    end: str | None = None,
    limit: int = 20,
) -> dict:
    """按 sql_fingerprint 聚合慢 SQL（P2-2）：同模板次数/平均耗时/P95/样本 SQL，
    避免 N+1 或全表扫描 SQL 因参数不同刷屏。"""
    end_dt = parse_time(end) or datetime.now()
    start_dt = parse_time(start) or (end_dt - timedelta(hours=1))
    rows = request.app.state.collector_repo.query_slow_sql_fingerprints(
        app=app, start=start_dt, end=end_dt,
        stress_task_id=stressTaskId, limit=limit)
    return _ok(rows)


@router.get("/slow-commands")
def list_slow_commands(
    request: Request,
    app: str | None = None,
    type: str | None = None,
    minCostMs: float | None = Query(default=None),
    stressTaskId: int | None = Query(default=None),
    start: str | None = None,
    end: str | None = None,
    size: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict:
    page_size = clamp_size(size, limit)
    cursor_pair, err = _cursor(cursor)
    if err:
        return err
    rows = request.app.state.collector_repo.query_slow_commands(
        app=app, cmd_type=type, min_cost_ms=minCostMs,
        start=parse_time(start), end=parse_time(end),
        cursor_pair=cursor_pair, limit_plus_one=page_size + 1,
        stress_task_id=stressTaskId,
    )
    return _ok(build_cursor_page(
        rows, page_size,
        time_getter=lambda r: r.execute_time,
        id_getter=lambda r: r.id,
        serializer=lambda r: {
            "id": r.id, "appName": r.app_name, "type": r.type,
            "command": r.command, "costMs": r.cost_ms, "traceId": r.trace_id,
            "methodChain": r.method_chain, "executeTime": _fmt(r.execute_time),
            "stressTaskId": r.stress_task_id,
        },
    ))


@router.get("/hotspots")
def list_hotspots(
    request: Request,
    app: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 30,
) -> dict:
    end_dt = parse_time(end) or datetime.now()
    start_dt = parse_time(start) or (end_dt - timedelta(hours=1))
    rows = request.app.state.collector_repo.query_hotspots(app, start_dt, end_dt, limit)
    return _ok(rows)


@router.get("/profiles")
def list_profiles(
    request: Request,
    app: str | None = None,
    profileType: str = Query(default="cpu", pattern="^(cpu|alloc|lock)$"),
    stressTaskId: int | None = Query(default=None),
    start: str | None = None,
    end: str | None = None,
    format: str = Query(default="folded", pattern="^(folded|tree)$"),
    maxNodes: int = Query(default=DEFAULT_MAX_NODES, ge=1, le=4000),
) -> dict:
    """JFR 折叠栈聚合查询（P2-1）：时间窗内多批快照按栈路径合并累加后返回。

    format=folded 返回标准折叠文本（火焰图组件直用）；format=tree 返回聚合树 JSON
    （超 maxNodes 聚入 *overflow* 桶，防前端卡顿）。
    """
    end_dt = parse_time(end) or datetime.now()
    start_dt = parse_time(start) or (end_dt - timedelta(hours=1))
    rows = request.app.state.collector_repo.query_profile_snapshots(
        app=app, profile_type=profileType, stress_task_id=stressTaskId,
        start=start_dt, end=end_dt)
    merged = merge_folded([r.folded_stacks for r in rows])
    stacks = merged["stacks"]
    payload: dict = {
        "appName": app,
        "profileType": profileType,
        "stressTaskId": stressTaskId,
        "window": {"start": _fmt(start_dt), "end": _fmt(end_dt)},
        "snapshotCount": len(rows),
        "totalSamples": merged["totalSamples"],
        "uniqueStacks": len(stacks),
    }
    if format == "tree":
        payload["tree"] = folded_to_tree(stacks, maxNodes)
    else:
        payload["folded"] = "\n".join(
            f"{path} {cnt}" for path, cnt in
            sorted(stacks.items(), key=lambda kv: kv[1], reverse=True))
    return _ok(payload)
