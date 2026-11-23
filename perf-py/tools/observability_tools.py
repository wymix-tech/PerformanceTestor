"""AI 观测查询工具（P1-2）：打破「AI 只能看离线慢日志」限制。

5 个只读工具（read_only=True），按 stressTaskId / app+时间窗拉取平台库中的
运行态证据，形成「现象 → 运行态证据 → 执行计划/源码 → 根因假设」证据链：

- jvm_metrics_query：JVM 指标<b>聚合摘要</b>（堆峰值/GC 增量/线程/CPU），
  绝不把 500 条原始时序塞进 LLM 上下文；
- slow_sql_query：慢 SQL Top N（costMs 降序，sqlText 截断 500 字符）；
  无染色命中时自动降级按压测任务时间窗查询并标注 matchedBy；
- trace_query：最慢调用链摘要 Top N（不含 traceJson 巨树，防 Token 溢出）；
- hotspot_query：CPU 热点方法 Top N（任务时间窗或最近 1 小时聚合）；
- db_explain_run：只读 EXPLAIN（经 DbExplainService 白名单校验；密码仅在
  取用点 decrypt，绝不入返回值与日志）。

装配：server/app.py 在 AiAgentService 之前 build_observability_tools(...)，
经 registry.setdefault 注入（不覆盖测试注入的桩工具）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from tools.registry import Tool

# Top N 与文本截断上限（防返回体撑爆 LLM 上下文窗口）
DEFAULT_TOP_N = 10
MAX_TOP_N = 30
HOTSPOT_TOP_N = 15
SQL_TEXT_LIMIT = 500
# JVM 时序聚合的最大采样点数（超出仅影响增量口径精度，不影响峰值）
JVM_WINDOW_SAMPLES = 1000


def _positive_int(v) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _clamp_limit(v, default: int, upper: int) -> int:
    n = _positive_int(v)
    return min(n, upper) if n else default


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def _resolve_task_window(stress_repo, stress_task_id) -> dict | None:
    """压测任务 → {app, start, end, task}；任务不存在返回 None。

    时间窗：[create_time - 5s, finish_time + 10s]（finish 未落时用 now 兜底），
    覆盖探针上报与落库的少量延迟。
    """
    task = stress_repo.find_by_id(stress_task_id)
    if task is None:
        return None
    start = task.create_time - timedelta(seconds=5)
    end = (task.finish_time or datetime.now()) + timedelta(seconds=10)
    return {"app": task.app_name, "start": start, "end": end, "task": task}


def _match_conn(collector_repo, app: str | None, db_host: str | None):
    """按 match_order() 三级匹配连接：host 双向包含 → 应用绑定 → 默认兜底。"""
    conns = collector_repo.match_order()
    if not conns:
        return None
    if db_host:
        for c in conns:
            if c.host and (db_host in c.host or c.host in db_host):
                return c
    if app:
        for c in conns:
            if c.app_name == app:
                return c
    return conns[0]


def build_observability_tools(collector_repo, stress_repo,
                              db_explain) -> list[Tool]:
    """构造 5 个只读观测工具（依赖注入：collector/stress 仓储 + EXPLAIN 服务）。"""

    # ---------------------------------------------------------- jvm_metrics_query

    def jvm_metrics_query(args: dict) -> dict:
        task_id = _positive_int(args.get("stressTaskId"))
        app = args.get("app") or None
        minutes = _positive_int(args.get("minutes")) or 15
        matched_by = None
        start = end = None
        if task_id is not None:
            win = _resolve_task_window(stress_repo, task_id)
            if win is None:
                return {"error": f"stress task not found: {task_id}"}
            app, start, end = win["app"], win["start"], win["end"]
            matched_by = "stress_task"
        elif app:
            end = datetime.now()
            start = end - timedelta(minutes=minutes)
            matched_by = "app_window"
        else:
            return {"error": "需要 stressTaskId 或 app 参数之一"}

        rows = collector_repo.query_jvm_metrics(
            app=app, start=start, end=end,
            cursor_pair=None, limit_plus_one=JVM_WINDOW_SAMPLES)
        if not rows:
            return {"error": "时间窗内无 JVM 指标", "matchedBy": matched_by,
                    "app": app, "window": {"start": _iso(start), "end": _iso(end)}}
        # query_jvm_metrics 返回时间倒序，聚合增量前转为正序
        seq = sorted(rows, key=lambda r: (r.collect_time, r.id))
        heap_peak = max(r.heap_used_mb for r in seq)
        heap_max = max((r.heap_max_mb for r in seq), default=0.0)
        first, last = seq[0], seq[-1]
        return {
            "matchedBy": matched_by,
            "app": app,
            "stressTaskId": task_id,
            "window": {"start": _iso(start), "end": _iso(end)},
            "samples": len(seq),
            "heap": {
                "peakUsedMb": round(heap_peak, 1),
                "maxMb": round(heap_max, 1),
                "peakUsagePct": round(heap_peak * 100.0 / heap_max, 1) if heap_max else None,
            },
            "gc": {
                "youngGcCountDelta": max(0, last.young_gc_count - first.young_gc_count),
                "youngGcMsDelta": max(0, last.young_gc_ms - first.young_gc_ms),
                "fullGcCountDelta": max(0, last.full_gc_count - first.full_gc_count),
                "fullGcMsDelta": max(0, last.full_gc_ms - first.full_gc_ms),
            },
            "threads": {"peakCount": max(r.thread_count for r in seq)},
            "cpu": {
                "jvmPeakPct": round(max(r.cpu_usage for r in seq), 1),
                "systemPeakPct": round(max(r.system_cpu for r in seq), 1),
            },
        }

    # ---------------------------------------------------------- slow_sql_query

    def slow_sql_query(args: dict) -> dict:
        task_id = _positive_int(args.get("stressTaskId"))
        app = args.get("app") or None
        min_cost = args.get("minCostMs")
        limit = _clamp_limit(args.get("limit"), DEFAULT_TOP_N, MAX_TOP_N)
        matched_by = None
        rows = []
        if task_id is not None:
            rows = collector_repo.query_slow_sqls(
                app=app, min_cost_ms=min_cost, start=None, end=None,
                cursor_pair=None, limit_plus_one=200, stress_task_id=task_id)
            matched_by = "stress_task"
            if not rows:
                # 降级：旧探针未染色时按任务时间窗查询（标注 matchedBy）
                win = _resolve_task_window(stress_repo, task_id)
                if win is None:
                    return {"error": f"stress task not found: {task_id}"}
                rows = collector_repo.query_slow_sqls(
                    app=win["app"], min_cost_ms=min_cost,
                    start=win["start"], end=win["end"],
                    cursor_pair=None, limit_plus_one=200)
                matched_by = "time_window_fallback"
        else:
            end = datetime.now()
            rows = collector_repo.query_slow_sqls(
                app=app, min_cost_ms=min_cost,
                start=end - timedelta(minutes=_positive_int(args.get("minutes")) or 60),
                end=end, cursor_pair=None, limit_plus_one=200)
            matched_by = "app_window"
        top = sorted(rows, key=lambda r: r.cost_ms, reverse=True)[:limit]
        return {
            "matchedBy": matched_by,
            "total": len(rows),
            "items": [{
                "id": r.id,
                "appName": r.app_name,
                "sqlText": (r.sql_text or "")[:SQL_TEXT_LIMIT],
                "costMs": r.cost_ms,
                "dbHost": r.db_host,
                "executeTime": _iso(r.execute_time),
                "stressTaskId": r.stress_task_id,
            } for r in top],
        }

    # ---------------------------------------------------------- trace_query

    def trace_query(args: dict) -> dict:
        task_id = _positive_int(args.get("stressTaskId"))
        app = args.get("app") or None
        method = args.get("method") or None
        min_cost = args.get("minCostMs")
        limit = _clamp_limit(args.get("limit"), DEFAULT_TOP_N, MAX_TOP_N)
        matched_by = None
        if task_id is not None:
            win = _resolve_task_window(stress_repo, task_id)
            if win is None:
                return {"error": f"stress task not found: {task_id}"}
            rows = collector_repo.query_traces(
                app=app or win["app"], method=method, min_cost_ms=min_cost,
                start=win["start"], end=win["end"],
                cursor_pair=None, limit_plus_one=200, stress_task_id=task_id)
            matched_by = "stress_task"
            if not rows:
                # 降级：按任务时间窗（不限定染色）查询
                rows = collector_repo.query_traces(
                    app=win["app"], method=method, min_cost_ms=min_cost,
                    start=win["start"], end=win["end"], cursor_pair=None,
                    limit_plus_one=200)
                matched_by = "time_window_fallback"
        else:
            end = datetime.now()
            rows = collector_repo.query_traces(
                app=app, method=method, min_cost_ms=min_cost,
                start=end - timedelta(minutes=_positive_int(args.get("minutes")) or 60),
                end=end, cursor_pair=None, limit_plus_one=200)
            matched_by = "app_window"
        top = sorted(rows, key=lambda r: r.total_ms, reverse=True)[:limit]
        return {
            "matchedBy": matched_by,
            "total": len(rows),
            "items": [{
                "traceId": r.trace_id,
                "rootMethod": r.root_method,
                "totalMs": r.total_ms,
                "startTime": _iso(r.start_time),
                "stressTaskId": r.stress_task_id,
            } for r in top],
        }

    # ---------------------------------------------------------- hotspot_query

    def hotspot_query(args: dict) -> dict:
        task_id = _positive_int(args.get("stressTaskId"))
        app = args.get("app") or None
        limit = _clamp_limit(args.get("limit"), HOTSPOT_TOP_N, 200)
        if task_id is not None:
            win = _resolve_task_window(stress_repo, task_id)
            if win is None:
                return {"error": f"stress task not found: {task_id}"}
            app = app or win["app"]
            start, end = win["start"], win["end"]
            matched_by = "stress_task"
        else:
            end = datetime.now()
            start = end - timedelta(minutes=_positive_int(args.get("minutes")) or 60)
            matched_by = "app_window"
        rows = collector_repo.query_hotspots(app, start, end, limit)
        return {
            "matchedBy": matched_by,
            "window": {"start": _iso(start), "end": _iso(end)},
            "items": rows,
        }

    # ---------------------------------------------------------- db_explain_run

    def db_explain_run(args: dict) -> dict:
        sql = args.get("sql")
        if not sql or not str(sql).strip():
            return {"error": "需要 sql 参数"}
        app = args.get("app") or None
        db_host = args.get("dbHost") or None
        conn = _match_conn(collector_repo, app, db_host)
        if conn is None:
            return {"error": "未找到匹配的数据库连接（请先在平台配置数据库连接）"}
        # 密码仅在取用点解密（AES-GCM），明文不进日志与返回值
        from server.crypto import decrypt

        explain = db_explain.analyze_sql(
            conn.db_url, conn.username, decrypt(conn.password), str(sql))
        if explain.get("error"):
            return {"error": explain["error"], "connName": conn.conn_name}
        return {
            "connName": conn.conn_name,
            "dbHost": db_explain.url_host(conn.db_url),
            "summary": db_explain.format_summary(explain),
            "indexes": explain.get("indexes"),
            "tableRows": explain.get("tableRows"),
        }

    def _read_only(name: str, desc: str, params: dict,
                   handler: Callable[[dict], dict]) -> Tool:
        return Tool(name=name, description=desc, parameters=params, handler=handler)

    return [
        _read_only(
            "jvm_metrics_query",
            "按压测任务 ID（自动解析应用与时间窗）或应用+分钟数聚合查询 JVM 运行态摘要："
            "堆峰值与占比、Young/Full GC 增量次数与耗时、线程峰值、JVM/系统 CPU 峰值。"
            "只返回聚合摘要，不返回原始时序点。",
            {"type": "object", "properties": {
                "stressTaskId": {"type": "integer", "description": "压测任务 ID（优先）"},
                "app": {"type": "string", "description": "应用名（无任务 ID 时按最近 N 分钟查询）"},
                "minutes": {"type": "integer", "description": "回溯分钟数，默认 15"},
            }, "additionalProperties": False},
            jvm_metrics_query),
        _read_only(
            "slow_sql_query",
            "查询压测期间触发的慢 SQL Top N（按 costMs 降序，sqlText 截断 500 字符）。"
            "按 stressTaskId 精确匹配；旧探针未染色时自动降级为该任务时间窗查询"
            "（返回体标注 matchedBy=time_window_fallback）。",
            {"type": "object", "properties": {
                "stressTaskId": {"type": "integer"},
                "app": {"type": "string"},
                "minCostMs": {"type": "number"},
                "limit": {"type": "integer", "description": "Top N，默认 10，上限 30"},
            }, "additionalProperties": False},
            slow_sql_query),
        _read_only(
            "trace_query",
            "查询最慢调用链摘要 Top N（traceId/根方法/总耗时/开始时间，不含调用树明细）。"
            "按 stressTaskId 或 app+method+minCostMs 过滤。",
            {"type": "object", "properties": {
                "stressTaskId": {"type": "integer"},
                "app": {"type": "string"},
                "method": {"type": "string", "description": "根方法名模糊匹配"},
                "minCostMs": {"type": "number"},
                "limit": {"type": "integer"},
            }, "additionalProperties": False},
            trace_query),
        _read_only(
            "hotspot_query",
            "按压测任务时间窗或最近 1 小时聚合查询 CPU 热点方法 Top N"
            "（方法名 + 采样数，默认 15）。",
            {"type": "object", "properties": {
                "stressTaskId": {"type": "integer"},
                "app": {"type": "string"},
                "minutes": {"type": "integer"},
                "limit": {"type": "integer"},
            }, "additionalProperties": False},
            hotspot_query),
        _read_only(
            "db_explain_run",
            "对一条 SQL 执行只读 EXPLAIN 检测（仅 SELECT/WITH，经白名单校验防注入）："
            "返回执行计划摘要、涉及表索引与行数。连接按 app/dbHost 三级匹配平台数据库连接配置。",
            {"type": "object", "properties": {
                "sql": {"type": "string", "description": "待 EXPLAIN 的 SELECT/WITH 语句"},
                "app": {"type": "string", "description": "应用名（连接匹配）"},
                "dbHost": {"type": "string", "description": "库地址（连接匹配，host:port/db）"},
            }, "required": ["sql"], "additionalProperties": False},
            db_explain_run),
    ]


__all__ = ["build_observability_tools", "wire_observability_tools"]


def wire_observability_tools(registry: dict, session_factory=None) -> dict:
    """按平台库装配观测工具并 setdefault 注入（server/app.py 与 CLI agent 共用）。

    session_factory 缺省时按 database_url() 建库（CLI 场景）；已存在的同名工具
    （测试桩/显式注入）不覆盖。
    """
    if session_factory is None:
        from server.db import database_url, make_session_factory

        session_factory = make_session_factory(database_url())
    from server.collector_repo import CollectorRepository
    from server.db_explain import DbExplainService
    from server.stress_repo import StressRepository

    for tool in build_observability_tools(
            CollectorRepository(session_factory),
            StressRepository(session_factory),
            DbExplainService()):
        registry.setdefault(tool.name, tool)
    return registry
