"""响应读取公共工具：移植自 ResponseAccess.java。

响应头取值与响应体 JSON 解析收敛到一处（断言侧与提取侧共用），
避免实现分叉导致「断言通过但提取失败」这类问题。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 解析数组下标：仅接受无符号十进制数字（允许前导零，如 01）
_INDEX_RE = re.compile(r"^\d+$")


def header_value(headers: dict[str, list[str]] | None, name: str | None) -> str | None:
    """响应头取值（头名大小写不敏感；多个同名头取第一个）。未命中返回 None。"""
    if not headers or not name or not name.strip():
        return None
    target = name.strip()
    for key, values in headers.items():
        if key is not None and key.lower() == target.lower() and values:
            return values[0]
    return None


def parse_json(body: str | None, max_body_chars: int) -> Any | None:
    """解析响应体为 JSON 树（超长截断）。非 JSON 返回 None 而不抛异常。"""
    if body is None or not body.strip():
        return None
    try:
        text = body[:max_body_chars] if len(body) > max_body_chars else body
        return json.loads(text)
    except ValueError:
        return None


def resolve_path(root: Any, path: str) -> Any | None:
    """按点分路径取值（如 data.token）。

    数组下标：当前节点为 list 且段为纯数字时按下标取（如 list.0.id）；
    「key 恰为数字串」的 dict 仍按 key 取值（严格限定在 list 分支内）。
    越界、负号、非数字段、对标量节点继续下钻——一律返回 None。
    """
    current = root
    for segment in path.split("."):
        if not segment or current is None:
            return None
        if isinstance(current, list):
            if not _INDEX_RE.match(segment):
                return None
            idx = int(segment)
            if idx >= len(current) or idx < 0:
                return None
            current = current[idx]
        elif isinstance(current, dict):
            current = current.get(segment)
        else:
            # 对标量节点继续下钻 → 未命中
            return None
    return current


def value_to_text(node: Any) -> str:
    """JSON 节点 → 字符串（对齐 Jackson asText/toString 语义）。

    标量：str 转换（bool → true/false，对齐 Jackson）；容器：紧凑 JSON 文本。
    """
    if isinstance(node, bool):
        return "true" if node else "false"
    if isinstance(node, (str, int, float)):
        return str(node)
    return json.dumps(node, separators=(",", ":"), ensure_ascii=False)


def is_scalar(node: Any) -> bool:
    return node is None or isinstance(node, (bool, str, int, float))
