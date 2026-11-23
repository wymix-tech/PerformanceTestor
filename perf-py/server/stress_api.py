"""stress 域 router（阶段 5 · 步骤 2 读路径；步骤 6 写路径）：契约对齐 Java StressController。

- GET /api/stress/tasks?status&limit      任务列表（时间倒序）
- GET /api/stress/tasks/{id}              任务详情
- GET /api/stress/trend?appName&url&limit 历史趋势（同应用+同 URL 已完成任务指标序列）
- GET /api/stress/tasks/{id}/samples      周期采样（实时图表，时间升序）
- GET /api/stress/tasks/{id}/report       压测报告 {task, result}
- POST /api/stress/tasks                  创建并启动压测（异步执行，立即返回任务）
- POST /api/stress/gate                   性能回归门禁（同步阻塞到完成，CI 用）
- POST /api/stress/jmx/import             导入 JMeter 脚本：解析为场景步骤 + 负载参数（不落库）
- POST /api/stress/jmx/export             导出压测方案为 JMX（请求体同创建任务；文件下载）
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Body, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from engine.jmeter_engine import generate_jmx
from server.jmx_import import MAX_JMX_BYTES, parse_jmx
from server.stress_repo import StressRepository
from server.stress_request import (
    parse_stress_request,
    to_scenario,
    validate_stress_request,
)

router = APIRouter(prefix="/api/stress")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _err(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


def _repo(request: Request) -> StressRepository:
    return request.app.state.stress_repo


# ---------------------------------------------------------------- 写路径


@router.post("/tasks")
async def start(request: Request) -> dict:
    """创建并启动压测任务（异步执行，立即返回任务）。

    对齐 Java：请求体为 StressTaskRequest；校验失败（IllegalArgumentException）
    → code=1 + 文案原样（无「服务器内部错误」前缀）；请求体反序列化失败走
    全局异常处理器（code=1 + 前缀）。
    """
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 — 对齐 HttpMessageNotReadableException 路径
        return {"code": 1, "message": f"服务器内部错误: {exc}", "data": None}
    if not isinstance(payload, dict):
        return {"code": 1, "message": "服务器内部错误: 请求体必须是 JSON 对象", "data": None}
    try:
        return _ok(request.app.state.stress_service.create_and_run(payload))
    except ValueError as exc:
        # 对齐 GlobalExceptionHandler.handleIllegalArgument：文案原样
        return _err(str(exc))


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, request: Request) -> dict:
    """中止运行中/排队中的压测任务（P1-3）：SIGTERM→SIGKILL，状态置 cancelled。

    任务已结束 / 运行器不支持取消 / 任务不存在 → code=1 + 文案原样。
    """
    try:
        return _ok(request.app.state.stress_service.cancel(task_id))
    except ValueError as exc:
        return _err(str(exc))


@router.post("/gate")
def gate(request: Request,
         payload: dict = Body(...),
         baseline_task_id: int | None = Query(None, alias="baselineTaskId"),
         timeout_sec: int | None = Query(None, alias="timeoutSec"),
         fail_on_no_baseline: bool = Query(False, alias="failOnNoBaseline"),
         webhook: str | None = None) -> dict:
    """性能回归门禁（CI 用）：创建压测 → 等待完成 → 基线比对 → 返回可判定结论。

    对齐 Java：同步阻塞到压测完成（timeoutSec 限制），HTTP 恒 200，
    流水线按 data.passed 判定退出码。校验失败 → code=1 + 文案原样。
    """
    try:
        return _ok(request.app.state.stress_service.run_gate(
            payload,
            baseline_task_id if baseline_task_id is not None else 0,
            timeout_sec if timeout_sec is not None else 0,
            fail_on_no_baseline,
            webhook,
            jvm_repo=getattr(request.app.state, "collector_repo", None),  # P2-3 资源维度
        ))
    except ValueError as exc:
        return _err(str(exc))


@router.post("/jmx/import")
async def import_jmx(file: UploadFile) -> dict:
    """导入 JMeter 测试计划：解析为场景步骤 + 负载参数（只解析不落库不发起）。

    与前端多接口混合表单对接：导入后回填步骤编辑再发起压测。
    仅接受 .jmx/.xml（根元素 jmeterTestPlan），≤2MB，UTF-8。
    """
    suffix = (file.filename or "").lower()
    if not suffix.endswith((".jmx", ".xml")):
        return _err("仅支持 .jmx / .xml 文件")
    data = await file.read()
    if len(data) > MAX_JMX_BYTES:
        return _err(f"JMX 文件过大（上限 {MAX_JMX_BYTES // 1048576}MB）")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _err(f"JMX 文件需为 UTF-8 编码：{exc}")
    try:
        return _ok(parse_jmx(content))
    except ValueError as exc:
        return _err(str(exc))


@router.post("/jmx/export")
def export_jmx(request: Request, payload: dict = Body(...)) -> Response:
    """导出压测方案为 JMeter 测试计划（请求体与创建任务一致，先校验再生成）。

    单接口模式直接导出；混合场景步骤按顺序写入（JMeter 顺序执行，
    权重配比与变量提取不生效——本地断言/变量上下文语义由平台引擎承担）。
    校验失败 → code=1 错误包装（前端按 JSON 解析）。
    """
    if not isinstance(payload, dict):
        return JSONResponse(status_code=200, content=_err("请求体必须是 JSON 对象"))
    try:
        req = parse_stress_request(payload)
        validate_stress_request(req)
        jmx = generate_jmx(to_scenario(req), steps_mode=req.is_scenario_mode)
    except ValueError as exc:
        return JSONResponse(status_code=200, content=_err(str(exc)))
    name = (req.task_name or "stress-plan").strip() or "stress-plan"
    safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name)[:60]
    # 中文文件名走 RFC 5987（Content-Disposition 头仅允许 latin-1）
    quoted = quote(f"{safe}.jmx")
    return Response(
        content=jmx,
        media_type="application/xml",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


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


@router.get("/tasks/{task_id}/dataset")
def dataset_binding(task_id: int, request: Request) -> dict:
    """T7.3 反查：压测任务 → 绑定数据集（Instance → Definition 版本 + Schema
    指纹 + 验证结果摘要全链可查；实施方案 §10.3 门禁 2）。

    未绑定的任务返回 data=null（非错误）。
    """
    repo = _repo(request)
    if repo.find_by_id(task_id) is None:
        return _err(f"stress task not found: {task_id}")
    binding = repo.dataset_binding(task_id)
    if binding is None:
        return _ok(None)
    try:
        instance = request.app.state.dataset_service.get_instance(
            binding["datasetInstanceId"], with_reports=True)
    except ValueError:
        instance = None
    return _ok({**binding, "dataset": instance})
