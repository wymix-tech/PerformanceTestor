"""凭据解密服务（阶段 5 · 步骤 7）：逐语义对齐 Java CryptoService。

db_conn.password 存储形如 `ENC:<base64(iv(12B) + ciphertext+tag)>`；
密钥 = SHA-256(PERF_CRYPTO_SECRET)（Java SecureRandom IV / GCM 128 位 tag 同构）。
降级路径与 Java 一致：值为空 / 无 ENC: 前缀（存量明文）/ Base64 非法 /
IV 长度不足 / GCM 校验失败（密钥轮换旧密文）→ 原样返回不抛异常。
未配置 PERF_CRYPTO_SECRET（Java perf.crypto.secret 默认空）→ 加密未启用，透传。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

log = logging.getLogger(__name__)

PREFIX = "ENC:"
IV_LENGTH = 12
TAG_BITS = 128

SECRET_ENV = "PERF_CRYPTO_SECRET"


def _aesgcm(secret: str):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - 环境差异路径
        raise RuntimeError(
            "已配置 PERF_CRYPTO_SECRET 但缺少 cryptography 依赖"
            "（uv sync --extra security）") from exc
    return AESGCM(hashlib.sha256(secret.encode("utf-8")).digest())


def secret_configured() -> bool:
    return bool(os.environ.get(SECRET_ENV, ""))


def decrypt(stored: str | None) -> str | None:
    """解密库中存储值（密文或存量明文；任何失败降级原值）。"""
    if not stored or not stored.startswith(PREFIX):
        return stored
    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        # Java 同款：未启用加密时 decrypt 原样返回（key 为 null 不会走到 AES）
        return stored
    try:
        packed = base64.b64decode(stored[len(PREFIX):])
        if len(packed) <= IV_LENGTH:
            log.warning("凭据密文长度异常（%s 字节），按原值处理", len(packed))
            return stored
        iv, ciphertext = packed[:IV_LENGTH], packed[IV_LENGTH:]
        plain = _aesgcm(secret).decrypt(iv, ciphertext, None)
        return plain.decode("utf-8")
    except Exception as exc:  # noqa: BLE001 — 密钥轮换/脏数据降级，不中断链路
        log.warning("凭据解密失败（可能是密钥已轮换或数据损坏），按原值处理: %s",
                    exc.__class__.__name__)
        return stored
