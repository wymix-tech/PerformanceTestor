"""定时压测调度路由：补齐 /api/stress/schedules（P0-1b）。

契约对齐 perf-web scheduleApi（index.ts）：
- GET  /api/stress/schedules              列表
- POST /api/stress/schedules              创建（{scheduleName, intervalMinutes,
                                          baselineTaskId, webhookUrl, task}）
- PUT  /api/stress/schedules/{id}/enabled 启停（?enabled=）
- DELETE /api/stress/schedules/{id}       删除
- GET  /api/stress/schedules/{id}/diff    基线对比（?taskId= 缺省上次触发任务）
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(prefix="/api/stress")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _err(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


def _service(request: Request):
    return request.app.state.schedule_service


@router.get("/schedules")
def list_schedules(request: Request, limit: int = 100) -> dict:
    rows = _service(request).repo.list(limit)
    return _ok([r.to_camel() for r in rows])


@router.post("/schedules")
def create_schedule(request: Request, body: dict = Body(...)) -> dict:
    try:
        row = _service(request).create(body)
    except ValueError as exc:
        return _err(str(exc))
    return _ok(row.to_camel())


@router.put("/schedules/{schedule_id}/enabled")
def toggle_schedule(request: Request, schedule_id: int,
                    enabled: bool = Query(...)) -> dict:
    row = _service(request).repo.set_enabled(schedule_id, enabled)
    if row is None:
        return _err(f"schedule not found: {schedule_id}")
    return _ok(row.to_camel())


@router.delete("/schedules/{schedule_id}")
def remove_schedule(request: Request, schedule_id: int) -> dict:
    if not _service(request).repo.delete(schedule_id):
        return _err(f"schedule not found: {schedule_id}")
    return _ok(None)


@router.get("/schedules/{schedule_id}/diff")
def schedule_diff(request: Request, schedule_id: int,
                  taskId: int | None = Query(None)) -> dict:
    try:
        return _ok(_service(request).diff(schedule_id, taskId))
    except ValueError as exc:
        return _err(str(exc))
