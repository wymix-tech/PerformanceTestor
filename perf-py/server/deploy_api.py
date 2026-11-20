"""deploy 域路由：补齐 /api/deploy/**（P0-1d）。

契约对齐 perf-web deployApi（index.ts）：SSH 连接管理/探活、平台侧构建、
远程启动、运行状态。DeployError → code=1 错误包装（HTTP 200）。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request

router = APIRouter(prefix="/api/deploy")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _err(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


def _svc(request: Request):
    return request.app.state.deploy_service


@router.get("/conns")
def list_conns(request: Request, limit: int = 50) -> dict:
    rows = request.app.state.deploy_conn_repo.list(limit)
    return _ok([r.to_camel() for r in rows])


@router.post("/conns")
def add_conn(request: Request, body: dict = Body(...)) -> dict:
    from server.deploy_repo import DeployConnModel

    if not body.get("connName") or not body.get("host"):
        return _err("connName 与 host 必填")
    try:
        row = request.app.state.deploy_conn_repo.insert(DeployConnModel(
            conn_name=str(body["connName"]), host=str(body["host"]),
            port=int(body.get("port") or 22),
            username=str(body.get("username") or "root"),
            auth_type=str(body.get("authType") or "password"),
            password=body.get("password"), private_key=body.get("privateKey"),
            work_dir=str(body.get("workDir") or "/opt/perftestor"),
            remark=body.get("remark")))
    except Exception as exc:  # noqa: BLE001 — 唯一键冲突等落库错误
        return _err(f"保存失败：{exc}")
    return _ok(row.to_camel())


@router.post("/conns/test")
def test_conn(request: Request, body: dict = Body(...)) -> dict:
    try:
        return _ok(_svc(request).test_conn(body))
    except Exception as exc:  # noqa: BLE001 — 连通性失败为业务结果
        return _err(str(exc))


@router.post("/conns/{conn_id}/test")
def test_conn_by_id(request: Request, conn_id: int) -> dict:
    try:
        return _ok(_svc(request).test_conn_by_id(conn_id))
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


@router.delete("/conns/{conn_id}")
def del_conn(request: Request, conn_id: int) -> dict:
    if not request.app.state.deploy_conn_repo.delete(conn_id):
        return _err(f"deploy conn not found: {conn_id}")
    return _ok(None)


@router.post("/build")
def build(request: Request, body: dict = Body(...)) -> dict:
    try:
        return _ok(_svc(request).build(int(body.get("repoId") or 0),
                                       body.get("module")))
    except Exception as exc:  # noqa: BLE001 — 构建/参数失败为业务结果
        return _err(str(exc))


@router.post("/start")
def start(request: Request, body: dict = Body(...)) -> dict:
    try:
        return _ok(_svc(request).start(
            conn_id=int(body.get("connId") or 0),
            jar_path=str(body.get("jarPath") or ""),
            agent_path=body.get("agentPath"),
            app_port=int(body.get("appPort") or 8081)))
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


@router.get("/status")
def status(request: Request, connId: int = Query(...),
           appPort: int = Query(8081)) -> dict:
    try:
        return _ok(_svc(request).status(connId, appPort))
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
