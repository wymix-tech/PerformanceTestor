"""datagen 域 router（阶段 5 · 步骤 4）：/api/datagen 只读端点契约对齐 DataGenController。

- GET /api/datagen/tasks?limit          任务列表（create_time 倒序，1..200 截断）
- GET /api/datagen/tasks/{id}           任务详情
- GET /api/datagen/suggestions          语义建议（repoId/tableName/hintType/status 可选过滤，
                                        confidence 倒序，固定 100 条）
- GET /api/datagen/plans?repoId&status&limit  方案列表（create_time,id 倒序）
- GET /api/datagen/plans/{id}           方案详情

写路径（tasks 启动 / tables / columns / suggestions adopt / plans analyze+execute+delete）
本轮不迁移：灌数执行依赖目标库连接与源码分析编排，读路径先行接管供前端列表/详情页切换。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from server.datagen_repo import DatagenRepository

router = APIRouter(prefix="/api/datagen")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _repo(request: Request) -> DatagenRepository:
    return request.app.state.datagen_repo


@router.get("/tasks")
def tasks(request: Request, limit: int = 50) -> dict:
    return _ok([t.to_camel() for t in _repo(request).tasks(limit)])


@router.get("/tasks/{task_id}")
def detail(task_id: int, request: Request) -> dict:
    task = _repo(request).task_by_id(task_id)
    return _ok(task.to_camel() if task is not None else None)


@router.get("/suggestions")
def suggestions(request: Request,
                repo_id: int | None = Query(None, alias="repoId"),
                table_name: str | None = Query(None, alias="tableName"),
                hint_type: str | None = Query(None, alias="hintType"),
                status: str | None = Query(None)) -> dict:
    rows = _repo(request).suggestions(repo_id, table_name, hint_type, status)
    return _ok([h.to_camel() for h in rows])


@router.get("/plans")
def plans(request: Request,
          repo_id: int | None = Query(None, alias="repoId"),
          status: str | None = Query(None),
          limit: int = 50) -> dict:
    return _ok([p.to_camel() for p in _repo(request).plans(repo_id, status, limit)])


@router.get("/plans/{plan_id}")
def plan_detail(plan_id: int, request: Request) -> dict:
    plan = _repo(request).plan_by_id(plan_id)
    return _ok(plan.to_camel() if plan is not None else None)
