"""慢 SQL 敏感参数脱敏与指纹归类（P2-2）。

仅在服务端命中慢 SQL 后处理（探针热路径零负担）：
- 脱敏：对单引号字符串中的手机号、身份证号、邮箱及 password/token/secret 赋值掩码；
- 指纹化：数字字面量、字符串字面量与 IN (...) 列表归一化为 ? 后取 SHA-1 前 16 位，
  同模板不同参数的慢 SQL 归为同一指纹（N+1 / 全表扫描不再按参数刷屏）。
"""

from __future__ import annotations

import hashlib
import re

# 敏感字面量模式（仅作用于单引号字符串内部）
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MASK = "***"

# password/token/secret 类赋值（含列名/键名形态，大小写不敏感）
_SECRET_ASSIGN = re.compile(
    r"(['\"`]?)(password|passwd|token|secret|api_key|apikey)\1\s*(=|:|like)\s*'[^']*'",
    re.IGNORECASE,
)

# 字面量归一化（先脱敏后指纹，保证顺序稳定）
_IN_LIST = re.compile(r"\bIN\s*\(\s*'(?:[^']|'')*'(?:\s*,\s*'(?:[^']|'')*)*\s*\)", re.IGNORECASE)
_IN_LIST_NUM = re.compile(r"\bIN\s*\(\s*\d+(?:\s*,\s*\d+)*\s*\)", re.IGNORECASE)
_IN_LIST_PH = re.compile(r"\bIN\s*\(\s*\?(?:\s*,\s*\?)+\s*\)", re.IGNORECASE)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_NUMBER_LITERAL = re.compile(r"\b\d+(\.\d+)?\b")
_MULTI_WHITESPACE = re.compile(r"\s+")


def mask_sensitive(sql: str) -> str:
    """敏感参数脱敏：手机号 / 身份证号 / 邮箱字面量与 password/token/secret 赋值。"""
    if not sql:
        return sql or ""

    def _mask_inner(match: re.Match) -> str:
        inner = match.group(0)[1:-1]
        masked = _PHONE.sub(_MASK, inner)
        masked = _ID_CARD.sub(_MASK, masked)
        masked = _EMAIL.sub(_MASK, masked)
        return "'" + masked + "'"

    # 先脱敏字符串字面量内部，再处理 password/token/secret 赋值形态
    masked = _STRING_LITERAL.sub(_mask_inner, sql)
    masked = _SECRET_ASSIGN.sub(
        lambda m: _STRING_LITERAL.sub("'" + _MASK + "'", m.group(0)), masked)
    return masked


def sql_fingerprint(sql: str) -> str:
    """SQL 指纹：字面量与 IN 列表归一化为 ? 后取 SHA-1 前 16 位 hex。"""
    normalized = mask_sensitive(sql or "")
    normalized = _IN_LIST.sub("IN (?)", normalized)
    normalized = _IN_LIST_NUM.sub("IN (?)", normalized)
    normalized = _IN_LIST_PH.sub("IN (?)", normalized)
    normalized = _STRING_LITERAL.sub("'?'", normalized)
    normalized = _NUMBER_LITERAL.sub("?", normalized)
    normalized = _MULTI_WHITESPACE.sub(" ", normalized).strip().lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
