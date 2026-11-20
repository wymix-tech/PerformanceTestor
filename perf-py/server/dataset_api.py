"""PTDE 数据集域 router（B7 · T7.2）：/api/datasets/* 端点族。

总方案 §15.2 端点集（前缀 /api/datasets）：
- POST /api/datasets/plan                 注册 Definition（spec 校验 + 指纹封存）
- GET  /api/datasets/plan?defKey&version  定义详情
- GET  /api/datasets/plans                定义列表
- POST /api/datasets                      创建 Instance（初始 PLANNED）
- GET  /api/datasets                      实例列表（游标分页 CursorPage + status 过滤）
- GET  /api/datasets/{id}                 实例详情（含验证报告，withReports=true）
- GET  /api/datasets/{id}/manifest        执行清单（§12.4 可复现要素，B7 续落库）
- GET  /api/datasets/{id}/progress        构建进度（状态 + 任务运行态 + 转换记录）
- POST /api/datasets/{id}/build           构建启动（异步；dryRun/seed/batch/resume）
- POST /api/datasets/{id}/verify          验证启动（异步）
- POST /api/datasets/{id}/cleanup         清理（决策 + CLEANUP 授权 + 审计）
- POST /api/datasets/{id}/cancel          取消（PLANNED/GENERATING/LOADING → CANCELLED）
- GET  /api/datasets/{id}/task            任务运行态查询（幂等/轮询辅助）

响应形态对齐既有域：{code, message, data}；领域失败（ValueError 族）
→ code=1 + 文案原样。**不触碰 /api/datagen 契约**（T7.2 验收约束）。

权限/审计：JWT 拦截由 app 工厂统一生效（/api/** 白名单外全拦截）；
审计经状态机 transition_log（operator/reason/occurred_at 随 Instance 持久化）
与清理三级审计（SecurityGate）落库。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request

from ptde.lifecycle.cleanup import CleanupBlocked
from server.ptde_repo import PtdeDatasetRepository

router = APIRouter(prefix="/api/datasets")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _err(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


def _repo(request: Request) -> PtdeDatasetRepository:
    return request.app.state.dataset_repo


def _service(request: Request):
    return request.app.state.dataset_service


# ---------------------------------------------------------------- Definition


@router.post("/plan")
async def register_plan(request: Request, payload: dict = Body(...)) -> dict:
    """注册 Definition（def_key+version 唯一；重复显式失败不覆盖）。"""
    try:
        def_key = payload.get("defKey")
        if not isinstance(def_key, str) or not def_key.strip():
            return _err("defKey 不能为空")
        version = payload.get("version") or "1"
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return _err("name 不能为空")
        spec = payload.get("spec")
        if not isinstance(spec, dict):
            return _err("spec 须为 DatasetSpec JSON 对象")
        return _ok(_service(request).register_definition(
            def_key.strip(), str(version), name.strip(), spec,
            created_by=payload.get("createdBy"),
            profile_fingerprint=payload.get("profileFingerprint"),
            seed=payload.get("seed"),
        ))
    except (ValueError, TypeError) as exc:
        return _err(str(exc))


@router.get("/plan")
def plan_detail(request: Request,
                def_key: str = Query(alias="defKey"),
                version: str = Query("1")) -> dict:
    try:
        return _ok(_service(request).get_definition(def_key, version))
    except ValueError as exc:
        return _err(str(exc))


@router.get("/plans")
def plans(request: Request, limit: int = 50) -> dict:
    return _ok(_service(request).list_definitions(limit))


@router.get("/plan/{def_key}/versions")
def plan_versions(def_key: str, request: Request) -> dict:
    return _ok(_service(request).list_versions(def_key))


# ---------------------------------------------------------------- Instance


@router.post("")
async def create_instance(request: Request, payload: dict = Body(...)) -> dict:
    """创建 Instance（同一 Definition 可建多实例；初始 PLANNED）。"""
    try:
        def_key = payload.get("defKey")
        if not isinstance(def_key, str) or not def_key.strip():
            return _err("defKey 不能为空")
        environment_id = payload.get("environmentId")
        if not isinstance(environment_id, str) or not environment_id.strip():
            return _err("environmentId 不能为空（目标环境须明确，§18.4 条件 8）")
        plan_id = payload.get("planId")
        return _ok(_service(request).create_instance(
            def_key.strip(), str(payload.get("version") or "1"),
            environment_id=environment_id,
            plan_id=int(plan_id) if plan_id is not None else None,
        ))
    except (ValueError, TypeError) as exc:
        return _err(str(exc))


@router.get("")
def instances(request: Request, status: str | None = None,
              size: int | None = None, cursor: str | None = None,
              limit: int | None = None) -> dict:
    """实例列表（游标分页 CursorPage：{items, nextCursor, hasMore}）。

    - cursor 传上页 nextCursor（不透明串），hasMore=false 表示末页；
    - size 每页条数（默认 50，上限 200）；limit 为兼容回退参数
      （deprecated，未传 size 时等价 size——前端 normalizePage 兜底口径）；
    - status 过滤与游标可组合。
    """
    effective_size = size if size is not None else (limit if limit is not None else 50)
    try:
        return _ok(_service(request).list_instances_page(
            status=status, size=effective_size, cursor=cursor))
    except ValueError as exc:
        return _err(str(exc))


@router.get("/{instance_id}")
def instance_detail(request: Request, instance_id: str,
                    with_reports: bool = Query(False, alias="withReports")) -> dict:
    try:
        return _ok(_service(request).get_instance(
            instance_id, with_reports=with_reports))
    except ValueError as exc:
        return _err(str(exc))


@router.get("/{instance_id}/manifest")
def instance_manifest(request: Request, instance_id: str) -> dict:
    """执行清单（最近一次构建）：§12.4 可复现要素 + 分片摘要（全链可溯）。"""
    try:
        return _ok(_service(request).instance_manifest(instance_id))
    except ValueError as exc:
        return _err(str(exc))


@router.get("/{instance_id}/progress")
def progress(request: Request, instance_id: str) -> dict:
    """构建进度：实例状态 + 任务运行态 + 转换记录（可追溯链路）。"""
    try:
        service = _service(request)
        detail = service.get_instance(instance_id)
        task = service.task_status(instance_id)
        return _ok({
            "taskId": task["taskId"],
            "running": task["running"],
            "instanceStatus": detail["status"],
            "validationSummary": detail["validationSummary"],
            "errorMsg": detail["errorMsg"],
            "transitionLog": detail["transitionLog"],
        })
    except ValueError as exc:
        return _err(str(exc))


@router.get("/{instance_id}/task")
def task_status(request: Request, instance_id: str) -> dict:
    try:
        return _ok(_service(request).task_status(instance_id))
    except ValueError as exc:
        return _err(str(exc))


# ---------------------------------------------------------------- 构建/验证/清理


@router.post("/{instance_id}/build")
async def build(request: Request, instance_id: str,
                dry_run: bool = Query(False, alias="dryRun"),
                seed: int | None = Query(None),
                batch: int | None = Query(None),
                resume: bool = Query(False),
                payload: dict | None = Body(None)) -> dict:
    """构建启动（异步执行于 ptde-dataset 线程池，不占 HTTP 线程）。

    dryRun=true 试运行；seed 透传生成内核（QW1 可复现）；batch 分片大小；
    resume=true 恢复 PARTIAL 实例（须 body.manifestId 携带恢复上下文）。
    """
    body = payload if isinstance(payload, dict) else {}
    try:
        return _ok(_service(request).build(
            instance_id, dry_run=dry_run, seed=seed, batch=batch,
            resume=resume, manifest_id=body.get("manifestId")))
    except ValueError as exc:
        return _err(str(exc))


@router.post("/{instance_id}/verify")
def verify(request: Request, instance_id: str) -> dict:
    """验证启动（异步）：五层验证 + READY 门禁终局（READY/FAILED 经状态机）。"""
    try:
        return _ok(_service(request).verify(instance_id))
    except ValueError as exc:
        return _err(str(exc))


@router.post("/{instance_id}/cleanup")
async def cleanup(request: Request, instance_id: str,
                  payload: dict = Body(...)) -> dict:
    """清理：档位决策 → CLEANING → CLEANUP 授权执行 → CLEANED（决策与执行分离）。

    请求体：{envClass, approvals:[{operation,target,authorizedBy,ticket}],
    referencedByOthers/partitioned/rowsToClean/batchPlanReady, executor 可注入}。
    生产/未知环境在 plan_cleanup 即被阻止（§13.1）。
    """
    try:
        env_class = payload.get("envClass")
        if not isinstance(env_class, str) or not env_class.strip():
            return _err("envClass 不能为空（环境类别须明确，§13.1）")
        approvals = payload.get("approvals") or []
        if not isinstance(approvals, list):
            return _err("approvals 须为数组")
        return _ok(_service(request).cleanup(
            instance_id, env_class=env_class.strip(),
            approvals=approvals,
            referenced_by_others=bool(payload.get("referencedByOthers")),
            partitioned=bool(payload.get("partitioned")),
            rows_to_clean=int(payload.get("rowsToClean") or 0),
            batch_plan_ready=bool(payload.get("batchPlanReady")),
        ))
    except (ValueError, TypeError) as exc:
        return _err(str(exc))
    except CleanupBlocked as exc:
        return _err(str(exc))


@router.post("/{instance_id}/cancel")
async def cancel(request: Request, instance_id: str,
                 payload: dict = Body(...)) -> dict:
    """取消（运行中/待构建实例主动取消，§15.2 取消语义；B7 续批次 3）。

    - 仅 PLANNED/GENERATING/LOADING 可取消（§3.5 出边评审结论：三条
      CANCELLED 出边自 R0 即在；VERIFYING/READY/终态/PARTIAL 显式拒绝）；
    - 请求体 {operator, reason} 必填——经状态机唯一入口 transition() 流转，
      transition_log 落库即审计，禁止绕过；
    - 执行中任务协作式取消：取消事件经 ctx 触达 pipeline generate 段，
      段落边界/分片间安全点协作退出（不强制 kill）；
    - 幂等：已 CANCELLED 返回 accepted=false，不二次流转。
    """
    try:
        operator = payload.get("operator")
        if not isinstance(operator, str) or not operator.strip():
            return _err("operator 不能为空（取消须经状态机留痕审计）")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return _err("reason 不能为空（取消须经状态机留痕审计）")
        return _ok(_service(request).cancel(
            instance_id, operator=operator.strip(), reason=reason.strip()))
    except ValueError as exc:
        return _err(str(exc))
