"""datagen 域 router（阶段 5 · 步骤 4 读路径；步骤 7 写路径）：契约对齐 DataGenController。

- GET /api/datagen/tasks?limit          任务列表（create_time 倒序，1..200 截断）
- GET /api/datagen/tasks/{id}           任务详情
- GET /api/datagen/suggestions          语义建议（repoId/tableName/hintType/status 可选过滤，
                                        confidence 倒序，固定 100 条）
- GET /api/datagen/plans?repoId&status&limit  方案列表（create_time,id 倒序）
- GET /api/datagen/plans/{id}           方案详情
- POST /api/datagen/tasks               创建并启动灌数任务（异步执行，connId 可覆盖手填连接）
- POST /api/datagen/tables              目标库表清单（灌数表单下拉）
- POST /api/datagen/columns             目标表列清单（含枚举建议/疑似枚举标记）
- POST /api/datagen/suggestions/{id}/adopt  建议采纳/忽略

plans 写路径（analyze/adopt/execute/delete）随步骤 8 全链迁移（PlanIntegrityChecker/
SeqStartResolver/RowCountEstimator 等管线移植后一并收口）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from server.datagen_repo import DatagenRepository

router = APIRouter(prefix="/api/datagen")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _err(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


def _repo(request: Request) -> DatagenRepository:
    return request.app.state.datagen_repo


def _service(request: Request):
    return request.app.state.datagen_service


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


# ---------------------------------------------------------------- 写路径


@router.post("/tasks")
async def start(request: Request) -> dict:
    """创建并启动灌数任务（异步执行；connId 提供时复用连接配置凭据覆盖手填值）。

    对齐 Java：校验失败（IllegalArgumentException）→ code=1 + 文案原样。
    """
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 — 反序列化失败走全局错误形态
        return {"code": 1, "message": f"服务器内部错误: {exc}", "data": None}
    if not isinstance(payload, dict):
        return {"code": 1, "message": "服务器内部错误: 请求体必须是 JSON 对象", "data": None}
    try:
        _service(request).apply_conn(payload)
        return _ok(_service(request).create_and_run(payload))
    except ValueError as exc:
        return _err(str(exc))


@router.post("/tables")
async def tables(request: Request) -> dict:
    """目标库表清单（connId 提供时复用连接配置凭据；Map 形态请求）。"""
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "message": f"服务器内部错误: {exc}", "data": None}
    if not isinstance(body, dict):
        return {"code": 1, "message": "服务器内部错误: 请求体必须是 JSON 对象", "data": None}
    try:
        _service(request).apply_conn(body)
        return _ok(_service(request).list_tables(
            body.get("dbUrl"), body.get("dbUser"), body.get("dbPassword")))
    except ValueError as exc:
        return _err(str(exc))


@router.post("/columns")
async def columns(request: Request) -> dict:
    """目标表列清单（含枚举建议值 suggestedValues 与疑似枚举标记 suspectEnum）。"""
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "message": f"服务器内部错误: {exc}", "data": None}
    if not isinstance(body, dict):
        return {"code": 1, "message": "服务器内部错误: 请求体必须是 JSON 对象", "data": None}
    try:
        _service(request).apply_conn(body)
        return _ok(_service(request).list_columns(
            body.get("dbUrl"), body.get("dbUser"), body.get("dbPassword"),
            body.get("tableName")))
    except ValueError as exc:
        return _err(str(exc))


@router.post("/suggestions/{hint_id}/adopt")
def adopt(hint_id: int, request: Request,
          adopt: bool = Query(True)) -> dict:
    """建议采纳/忽略（adopt=true 采纳为生成规则依据；返回 data=null）。"""
    try:
        _service(request).adopt(hint_id, adopt)
        return _ok(None)
    except ValueError as exc:
        return _err(str(exc))
