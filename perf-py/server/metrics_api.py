"""观测查询路由：补齐 /api/apps、/api/jvm/metrics、/api/traces、/api/slow-sqls、
/api/slow-commands、/api/hotspots。契约对齐 perf-web/src/api/index.ts。

四张时序表均为游标分页（(time_col DESC, id DESC) 复合键），
统一包装 {"code": 0, "message": "success", "data": ...}。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Query, Request

from server.cursor import build_cursor_page, clamp_size, decode_cursor, parse_time

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
        },
    ))


@router.get("/traces")
def list_traces(
    request: Request,
    app: str | None = None,
    method: str | None = None,
    minCostMs: float | None = Query(default=None),
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
    )
    return _ok(build_cursor_page(
        rows, page_size,
        time_getter=lambda r: r.start_time,
        id_getter=lambda r: r.id,
        serializer=lambda r: {
            "id": r.id, "traceId": r.trace_id, "appName": r.app_name,
            "rootMethod": r.root_method, "totalMs": r.total_ms,
            "startTime": _fmt(r.start_time),
        },
    ))


@router.get("/traces/{trace_id}")
def trace_detail(trace_id: str, request: Request) -> dict:
    row = request.app.state.collector_repo.get_trace_by_id(trace_id)
    if row is None:
        return {"code": 1, "message": f"trace not found: {trace_id}", "data": None}
    return _ok({
        "id": row.id, "traceId": row.trace_id, "appName": row.app_name,
        "rootMethod": row.root_method, "traceJson": row.trace_json,
        "totalMs": row.total_ms, "startTime": _fmt(row.start_time),
    })


@router.get("/slow-sqls")
def list_slow_sqls(
    request: Request,
    app: str | None = None,
    minCostMs: float | None = Query(default=None),
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
    )
    return _ok(build_cursor_page(
        rows, page_size,
        time_getter=lambda r: r.execute_time,
        id_getter=lambda r: r.id,
        serializer=lambda r: {
            "id": r.id, "appName": r.app_name, "sqlText": r.sql_text,
            "costMs": r.cost_ms, "source": r.source, "dbHost": r.db_host,
            "methodChain": r.method_chain, "executeTime": _fmt(r.execute_time),
        },
    ))


@router.get("/slow-commands")
def list_slow_commands(
    request: Request,
    app: str | None = None,
    type: str | None = None,
    minCostMs: float | None = Query(default=None),
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
    )
    return _ok(build_cursor_page(
        rows, page_size,
        time_getter=lambda r: r.execute_time,
        id_getter=lambda r: r.id,
        serializer=lambda r: {
            "id": r.id, "appName": r.app_name, "type": r.type,
            "command": r.command, "costMs": r.cost_ms, "traceId": r.trace_id,
            "methodChain": r.method_chain, "executeTime": _fmt(r.execute_time),
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
