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
    """JWT 签发与校验（HMAC 家族）：非法/过期返回 None（调用方按未登录处理）。

    算法选择对齐 jjwt 0.12 signWith(SecretKey) 行为：按密钥字节数自动升级——
    >=64 字节 HS512、>=48 字节 HS384、>=32 字节 HS256（默认密钥 59 字节实际出 HS384，
    双跑实测发现，Java 文档注释写 HS256 与实际行为不符）。校验侧接受整个 HMAC 家族，
    保证与任意密钥长度的 Java 侧签发互验。
    """

    _HMAC_ALGS = ("HS256", "HS384", "HS512")

    def __init__(self, props: AuthProperties):
        self._props = props

    def _secret_bytes(self) -> bytes:
        secret = self._props.secret.encode("utf-8")
        if len(secret) < 32:
            raise ValueError("perf.auth.secret 长度不足 32 字节，无法用于 HMAC 签名")
        return secret

    def _alg(self) -> str:
        n = len(self._secret_bytes())
        if n >= 64:
            return "HS512"
        if n >= 48:
            return "HS384"
        return "HS256"

    def issue(self, username: str) -> str:
        now = int(time.time())
        return pyjwt.encode(
            {"sub": username, "iat": now,
             "exp": now + self._props.expire_hours * 3600},
            self._secret_bytes(), algorithm=self._alg())

    def verify(self, token: str) -> str | None:
        try:
            claims = pyjwt.decode(token, self._secret_bytes(),
                                  algorithms=list(self._HMAC_ALGS))
            return claims.get("sub")
        except pyjwt.PyJWTError:
            return None
        except (ValueError, TypeError):
            return None
