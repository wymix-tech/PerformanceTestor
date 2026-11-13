"""响应断言评估：语义对齐 StressTaskRequest.Assertion。

全部断言命中才算请求成功；任一失败返回失败原因列表（供 Locust
response.failure / JMeter 判定使用）。

注意：这是 Phase 2 的最小实现，Phase 3 会将 Java 侧 ResponseAssertor
完整移植并做行为等价验证（golden 对比），届时此模块可能被替换/吸收。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from scenario.model import AssertionSpec


def _lookup_json(body: str, path: str) -> tuple[bool, Any]:
    """按点分路径从 JSON 体取值（对齐 Java 侧 json source 的语义边界）。"""
    try:
        data = json.loads(body)
    except ValueError:
        return False, None
    cur: Any = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _to_float(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def evaluate(assertions: list[AssertionSpec], status: int, body: str, headers: Mapping[str, str]) -> list[str]:
    """评估全部断言，返回失败原因列表（空列表 = 全部通过）。"""
    failures: list[str] = []
    headers_lower = {k.lower(): v for k, v in (headers or {}).items()}
    for a in assertions:
        if a.source == "status":
            actual: Any = status
        elif a.source == "body":
            actual = body
        elif a.source == "header":
            assert a.key
            if a.key.lower() not in headers_lower:
                failures.append(f"header[{a.key}] 不存在")
                continue
            actual = headers_lower[a.key.lower()]
        elif a.source == "json":
            assert a.key
            found, value = _lookup_json(body, a.key)
            if not found:
                failures.append(f"json[{a.key}] 路径不存在或响应非 JSON")
                continue
            actual = value
        else:  # pragma: no cover — pydantic Literal 已挡住非法值
            failures.append(f"未知断言来源: {a.source}")
            continue

        op, expected = a.op, a.expected
        passed = False
        if op == "eq":
            passed = str(actual) == expected
        elif op == "ne":
            passed = str(actual) != expected
        elif op == "contains":
            passed = expected in str(actual)
        elif op == "notContains":
            passed = expected not in str(actual)
        elif op in ("gt", "lt"):
            lhs, rhs = _to_float(actual), _to_float(expected)
            if lhs is None or rhs is None:
                failures.append(f"gt/lt 需要数值：actual={actual!r} expected={expected!r}")
                continue
            passed = lhs > rhs if op == "gt" else lhs < rhs
        elif op == "regex":
            passed = re.search(expected, str(actual)) is not None
        if not passed:
            failures.append(f"{a.source}{f'[{a.key}]' if a.key else ''} {op} {expected!r} 实际值 {actual!r}")
    return failures
