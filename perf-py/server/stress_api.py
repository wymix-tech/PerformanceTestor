"""stress 域 router（阶段 5 · 步骤 2）：/api/stress 只读端点契约对齐 Java StressController。

- GET /api/stress/tasks?status&limit      任务列表（时间倒序）
- GET /api/stress/tasks/{id}              任务详情
- GET /api/stress/trend?appName&url&limit 历史趋势（同应用+同 URL 已完成任务指标序列）
- GET /api/stress/tasks/{id}/samples      周期采样（实时图表，时间升序）
- GET /api/stress/tasks/{id}/report       压测报告 {task, result}

写路径（POST /tasks、/gate）本轮不迁移：压测执行仍由 Java perf-server 承担，
perf-py CLI `perf stress run` 已直连该契约（阶段 1），读路径先行接管供前端只读页面切换。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from server.stress_repo import StressRepository

router = APIRouter(prefix="/api/stress")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _err(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


def _repo(request: Request) -> StressRepository:
    return request.app.state.stress_repo


@router.get("/tasks")
def tasks(request: Request, status: str | None = None, limit: int = 50) -> dict:
    rows = _repo(request).query(status, limit)
    return _ok([r.to_camel() for r in rows])


@router.get("/tasks/{task_id}")
def detail(task_id: int, request: Request) -> dict:
    task = _repo(request).find_by_id(task_id)
    return _ok(task.to_camel() if task is not None else None)


@router.get("/trend")
def trend(request: Request, app_name: str = Query(alias="appName"),
          url: str = Query(), limit: int = 20) -> dict:
    """对齐 Java trend：同 appName+targetUrl 的 finished 任务，解析 resultJson 取
    核心指标，按 finishTime 正序，只保留最近 limit 条（仍正序）。"""
    repo = _repo(request)
    rows = repo.query_by_scene(app_name, url, "finished", 200)
    points = []
    for t in rows:
        result = repo.parse_result_json(t)
        if result is None:
            continue
        points.append({
            "taskId": t.id,
            "taskName": t.task_name,
            "finishTime": t.finish_time.isoformat(timespec="seconds")
                          if t.finish_time else None,
            "qps": result.get("qps") or 0,
            "p50Ms": result.get("p50Ms") or 0,
            "p95Ms": result.get("p95Ms") or 0,
            "p99Ms": result.get("p99Ms") or 0,
            "errorRate": result.get("errorRate") or 0,
            "totalRequests": result.get("totalRequests") or 0,
        })
    # 折线图从左到右即时间推进方向；超 limit 时只留最近 N 条（对齐 Java subList 尾截断）
    points.sort(key=lambda p: str(p["finishTime"]))
    if limit <= 0:
        points = []
    elif len(points) > min(limit, 100):
        points = points[len(points) - min(limit, 100):]
    return _ok(points)


@router.get("/tasks/{task_id}/samples")
def samples(task_id: int, request: Request, limit: int = 500) -> dict:
    rows = _repo(request).samples(task_id, limit)
    return _ok([s.to_camel() for s in rows])


@router.get("/tasks/{task_id}/report")
def report(task_id: int, request: Request) -> dict:
    repo = _repo(request)
    task = repo.find_by_id(task_id)
    if task is None:
        return _err(f"stress task not found: {task_id}")
    return _ok({"task": task.to_camel(), "result": repo.parse_result_json(task)})
