"""dbconn 域 router：数据库连接管理（契约对齐前端 dbConnApi 与原 Java DbConnController）。

- GET    /api/dbconns               连接列表（id ASC；密码不回显）
- POST   /api/dbconns               保存连接（id 提供 = 编辑；密码留空 = 沿用已存凭据）
- DELETE /api/dbconns/{id}          删除连接
- POST   /api/dbconns/test          连通性测试（可携带 SELECT 试跑 EXPLAIN）
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from server.dbconn_service import DbConnService

router = APIRouter(prefix="/api/dbconns")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _err(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


def _service(request: Request) -> DbConnService:
    return request.app.state.dbconn_service


@router.get("")
def list_conns(request: Request) -> dict:
    return _ok(_service(request).list_conns())


@router.post("")
async def save_conn(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 — 反序列化失败走全局错误形态
        return {"code": 1, "message": f"服务器内部错误: {exc}", "data": None}
    if not isinstance(payload, dict):
        return {"code": 1, "message": "服务器内部错误: 请求体必须是 JSON 对象", "data": None}
    try:
        return _ok(_service(request).save(payload))
    except ValueError as exc:
        return _err(str(exc))


@router.delete("/{conn_id}")
def delete_conn(conn_id: int, request: Request) -> dict:
    try:
        _service(request).delete(conn_id)
        return _ok(None)
    except ValueError as exc:
        return _err(str(exc))


@router.post("/test")
async def test_conn(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "message": f"服务器内部错误: {exc}", "data": None}
    if not isinstance(payload, dict):
        return {"code": 1, "message": "服务器内部错误: 请求体必须是 JSON 对象", "data": None}
    return _ok(_service(request).test(payload))
