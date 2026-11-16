"""auth 域服务（阶段 5 · 步骤 5）：轻量单管理员 JWT 鉴权，对齐 Java JwtService/拦截器。

- HS256，仅承载 subject（用户名），iat/exp 数值秒（jjwt 同形态，token 可跨侧互验）；
- 密钥不足 32 字节时签发即失败（对齐 Java 构造期校验）；
- 静态 API Token（X-Perf-Api-Token）与 JWT 并行生效，命中即放行并标记 CI 调用方；
- 拦截器白名单：/api/auth/**（登录本身）与 /api/agent/**（探针通道独立令牌）。
"""

from __future__ import annotations

import os
import time

import jwt as pyjwt

TOKEN_HEADER = "X-Perf-Api-Token"
AUTH_FAIL_BODY = {"code": 401, "message": "未登录或登录已过期", "data": None}

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"
DEFAULT_SECRET = "perf-testor-default-jwt-secret-please-change-in-production"
DEFAULT_EXPIRE_HOURS = 12


class AuthProperties:
    """对齐 Java AuthProperties（perf.auth.*，环境变量注入）。"""

    def __init__(self, enabled: bool | None = None, username: str | None = None,
                 password: str | None = None, secret: str | None = None,
                 expire_hours: int | None = None, api_token: str | None = None):
        # enabled：None 时读 PERF_AUTH_ENABLED（Java perf.auth.enabled 默认 true）
        self.enabled = enabled if enabled is not None \
            else os.environ.get("PERF_AUTH_ENABLED", "true").lower() != "false"
        self.username = username if username is not None \
            else os.environ.get("PERF_ADMIN_USER", DEFAULT_USERNAME)
        self.password = password if password is not None \
            else os.environ.get("PERF_ADMIN_PASSWORD", DEFAULT_PASSWORD)
        self.secret = secret if secret is not None \
            else os.environ.get("PERF_JWT_SECRET", DEFAULT_SECRET)
        self.expire_hours = expire_hours if expire_hours is not None \
            else int(os.environ.get("PERF_AUTH_EXPIRE_HOURS", DEFAULT_EXPIRE_HOURS))
        self.api_token = api_token if api_token is not None \
            else os.environ.get("PERF_API_TOKEN", "")
        self.agent_token = os.environ.get("PERF_AGENT_TOKEN", "")


class JwtService:
    """JWT 签发与校验（HS256）：非法/过期返回 None（调用方按未登录处理）。"""

    def __init__(self, props: AuthProperties):
        self._props = props

    def issue(self, username: str) -> str:
        secret_bytes = self._props.secret.encode("utf-8")
        if len(secret_bytes) < 32:
            raise ValueError("perf.auth.secret 长度不足 32 字节，无法用于 HS256 签名")
        now = int(time.time())
        return pyjwt.encode(
            {"sub": username, "iat": now,
             "exp": now + self._props.expire_hours * 3600},
            secret_bytes, algorithm="HS256")

    def verify(self, token: str) -> str | None:
        try:
            claims = pyjwt.decode(token, self._props.secret.encode("utf-8"),
                                  algorithms=["HS256"])
            return claims.get("sub")
        except pyjwt.PyJWTError:
            return None
        except (ValueError, TypeError):
            return None
