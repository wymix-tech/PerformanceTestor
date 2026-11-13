"""响应结果判断（断言）：移植自 ResponseAssertor.java。

- 全部断言命中才算成功（HTTP 200 ≠ 业务成功）
- 仅在存在 body/json 断言时才解析响应体（needs_body）
- 正则预编译缓存（热路径逐样本执行；非法正则缓存"编译失败"结果，不重复抛异常）
- 未知操作符按 eq 兜底，不因配置笔误让整轮压测全判失败

模型差异说明：Python 场景模型（pydantic Literal）在加载期即拒绝未知
source/op，Java 侧「未知来源忽略」兜底在 Python 侧不会触发；剩余语义
（头缺失/路径未命中时 ne|notContains 放行）逐行对齐。
"""

from __future__ import annotations

import re
import threading
from typing import Any

from scenario.model import AssertionSpec
from stress.response_access import parse_json, resolve_path, value_to_text

MAX_ASSERT_BODY_CHARS = 64 * 1024


def needs_body(rules: list[AssertionSpec] | None) -> bool:
    """是否需要读取响应体（存在 body/json 断言时需要）。"""
    if not rules:
        return False
    return any(a.source in ("body", "json") for a in rules)


_PATTERN_CACHE: dict[str, re.Pattern | None] = {}
_PATTERN_LOCK = threading.Lock()
MAX_CACHED_PATTERNS = 256


def _compile_cached(regex: str) -> re.Pattern | None:
    if regex in _PATTERN_CACHE:
        return _PATTERN_CACHE[regex]
    try:
        compiled: re.Pattern | None = re.compile(regex)
    except re.error:
        compiled = None
    with _PATTERN_LOCK:
        if len(_PATTERN_CACHE) >= MAX_CACHED_PATTERNS:
            _PATTERN_CACHE.clear()
        _PATTERN_CACHE[regex] = compiled
    return compiled


def _numeric_eq(a: str, e: str) -> bool:
    """相等比较：双方均为数值时按数值比较（避免 "200" 与 "200.0" 不等的误判）。"""
    try:
        return float(a.strip()) == float(e.strip())
    except ValueError:
        return a == e


def _numeric_compare(a: str, e: str) -> int:
    """数值比较：非数值退化为字符串字典序（保证有确定结果而非抛异常）。"""
    try:
        fa, fe = float(a.strip()), float(e.strip())
        return 0 if fa == fe else (1 if fa > fe else -1)
    except ValueError:
        if a == e:
            return 0
        return 1 if a > e else -1


def compare(op: str | None, actual: str | None, expected: str | None) -> bool:
    """单条比较；未知操作符按 eq 兜底。"""
    a = actual if actual is not None else ""
    e = expected if expected is not None else ""
    o = (op or "eq").strip().lower()
    if o == "eq":
        return _numeric_eq(a, e)
    if o == "ne":
        return not _numeric_eq(a, e)
    if o == "contains":
        return e in a
    if o == "notcontains":
        return e not in a
    if o == "gt":
        return _numeric_compare(a, e) > 0
    if o == "lt":
        return _numeric_compare(a, e) < 0
    if o == "regex":
        compiled = _compile_cached(e)
        # 正则非法：视为不命中，但不抛异常中断压测
        return compiled is not None and compiled.search(a) is not None
    return _numeric_eq(a, e)


def matches(
    rules: list[AssertionSpec] | None,
    status: int,
    headers: dict[str, list[str]] | None,
    body: str | None,
) -> bool:
    """执行全部断言：全部命中返回 True，任一不命中返回 False。"""
    if not rules:
        return True
    root: Any = None
    root_resolved = False
    for a in rules:
        if a.source == "status":
            actual = str(status)
        elif a.source == "header":
            actual = _header_of(headers, a.key)
            # 响应头缺失：除 ne / notContains 外一律判失败
            if actual is None:
                if (a.op or "eq").lower() in ("ne", "notcontains"):
                    continue
                return False
        elif a.source == "body":
            actual = body
            if actual is None:
                return False
        else:  # json
            if not root_resolved:
                root = parse_json(body, MAX_ASSERT_BODY_CHARS)
                root_resolved = True
            node = resolve_path(root, a.key or "") if root is not None else None
            if node is None:
                # 路径未命中：除 ne / notContains 外判失败
                if (a.op or "eq").lower() in ("ne", "notcontains"):
                    continue
                return False
            actual = value_to_text(node)
        if not compare(a.op, actual, a.expected):
            return False
    return True


def _header_of(headers: dict[str, list[str]] | None, name: str | None) -> str | None:
    from stress.response_access import header_value

    return header_value(headers, name)
