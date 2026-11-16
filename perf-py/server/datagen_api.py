"""datagen 域 router（阶段 5 · 步骤 4 读路径；步骤 7 写路径）：契约对齐 DataGenController。

- GET /api/datagen/tasks?limit          任务列表（create_time 倒序，1..200 截断）
- GET /api/datagen/tasks/{id}           任务详情
- GET /api/datagen/suggestions          语义建议（repoId/tableName/hintType/status 可选过滤，
                                        confidence 倒序，固定 100 条）
- GET /api/datagen/plans?repoId&status&limit  方案列表（create_time,id 倒序）
- GET /api/datagen/plans/{id}           方案详情
- POST /api/datagen/plans/analyze       接口驱动方案分析（异步执行于分析线程池）
- POST /api/datagen/plans/{id}/adopt    方案采纳/放弃
- POST /api/datagen/plans/{id}/execute  按方案执行灌数（dryRun=true 试运行）
- DELETE /api/datagen/plans/{id}        删除方案
- POST /api/datagen/tasks               创建并启动灌数任务（异步执行，connId 可覆盖手填连接）
- POST /api/datagen/tables              目标库表清单（灌数表单下拉）
- POST /api/datagen/columns             目标表列清单（含枚举建议/疑似枚举标记）
- POST /api/datagen/suggestions/{id}/adopt  建议采纳/忽略
- POST /api/datagen/plans/analyze       接口驱动方案分析（异步执行于分析线程池）
- POST /api/datagen/plans/{id}/adopt    方案采纳/放弃
- POST /api/datagen/plans/{id}/execute  按方案执行灌数（dryRun=true 试运行）
- DELETE /api/datagen/plans/{id}        删除方案

plans 写路径随步骤 8 全链迁移（PlanIntegrityChecker/SeqStartResolver/
RowCountEstimator 等管线见 datagen/plan_pipeline.py，编排见 datagen_plan_service.py）。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request

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


def _plan_service(request: Request):
    return request.app.state.datagen_plan_service


# ---------------------------------------------------------------- plans 写路径


@router.post("/plans/analyze")
async def analyze(request: Request) -> dict:
    """接口驱动方案分析：接口定位 → 调用链 → SQL → 访问事实 → 规则 → 行数 → 落库。

    对齐 Java：校验失败 → code=1 + 文案原样；分析异步执行于分析线程池，
    返回已落库的方案（status=pending，前端轮询 tablesJson 出现即为完成）。
    """
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "message": f"服务器内部错误: {exc}", "data": None}
    if not isinstance(payload, dict):
        return {"code": 1, "message": "服务器内部错误: 请求体必须是 JSON 对象", "data": None}
    try:
        return _ok(_plan_service(request).analyze(payload))
    except ValueError as exc:
        return _err(str(exc))


@router.post("/plans/{plan_id}/adopt")
def adopt_plan(plan_id: int, request: Request, adopt: bool = Query(True)) -> dict:
    """方案采纳/放弃（返回方案对象，status 为更新后值）。"""
    try:
        return _ok(_plan_service(request).adopt(plan_id, adopt).to_camel())
    except ValueError as exc:
        return _err(str(exc))


@router.post("/plans/{plan_id}/execute")
def execute_plan(plan_id: int, request: Request,
                 dry_run: bool = Query(True, alias="dryRun"),
                 payload: dict | None = Body(None)) -> dict:
    """按方案执行灌数（dryRun=true 仅试运行）。

    对齐 Java：请求体可选带 connId/dbUrl（分析时未指定连接则执行时补上）与
    rowOverrides:[{table,rowCount}]；仅当 body 真的带连接字段时才回填——
    只改行数的请求放行后由 runFromPlan 报「方案未指定连接」才是准确原因。
    """
    body = payload if isinstance(payload, dict) else {}
    plan_service = _plan_service(request)
    try:
        row_overrides = _parse_row_overrides(body)
        if _carries_connection(body) and plan_service.plan_needs_connection(plan_id):
            plan_service.apply_plan_connection(plan_id, body)
        return _ok(plan_service.run_from_plan(plan_id, dry_run, row_overrides))
    except ValueError as exc:
        return _err(str(exc))


@router.delete("/plans/{plan_id}")
def delete_plan(plan_id: int, request: Request) -> dict:
    try:
        _plan_service(request).delete(plan_id)
        return _ok(None)
    except ValueError as exc:
        return _err(str(exc))


def _carries_connection(body: dict | None) -> bool:
    """请求体是否携带连接字段（connId 或 dbUrl 任一有值；对齐 Controller 包级可见）。"""
    if not body:
        return False
    return _is_present(body.get("connId")) or _is_present(body.get("dbUrl"))


def _is_present(v) -> bool:
    return v is not None and str(v).strip() != ""


def _parse_row_overrides(body: dict | None) -> dict[str, int]:
    """解析用户在方案卡上改过的行数：{rowOverrides:[{table,rowCount}]}。"""
    out: dict[str, int] = {}
    raw = (body or {}).get("rowOverrides")
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict) or item.get("table") is None or item.get("rowCount") is None:
            continue
        name = str(item["table"]).strip().lower()
        try:
            v = int(item["rowCount"])
        except (TypeError, ValueError):
            continue
        if name and v > 0:
            out[name] = v
    return out


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
    try:
        # 对齐 Java planService.detail：不存在 → code=1「方案不存在: {id}」
        return _ok(_plan_service(request).detail(plan_id).to_camel())
    except ValueError as exc:
        return _err(str(exc))


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
