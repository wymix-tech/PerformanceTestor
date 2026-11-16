"""auth 域 router（阶段 5 · 步骤 5）：/api/auth 契约对齐 Java AuthController。

- POST /api/auth/login：单管理员登录（失败统一 401 业务码，不区分账号/密码错误防枚举）
- GET  /api/auth/me：会话探测 {username, authEnabled}
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from server.auth_service import AuthProperties

router = APIRouter(prefix="/api/auth")


def _props(request: Request) -> AuthProperties:
    return request.app.state.auth_props


def _err(code: int, message: str) -> dict:
    return {"code": code, "message": message, "data": None}


@router.post("/login")
def login(request: Request, body: dict) -> dict:
    props = _props(request)
    username = (body or {}).get("username")
    password = (body or {}).get("password")
    if not username or not str(username).strip() or not password or not str(password).strip():
        return _err(401, "请输入账号与密码")
    if props.username != str(username) or props.password != str(password):
        return _err(401, "账号或密码错误")
    token = request.app.state.jwt_service.issue(props.username)
    return {"code": 0, "message": "success",
            "data": {"token": token, "username": props.username,
                     "expireHours": props.expire_hours}}


@router.get("/me")
def me(request: Request) -> dict:
    props = _props(request)
    return {"code": 0, "message": "success",
            "data": {"username": props.username, "authEnabled": props.enabled}}
