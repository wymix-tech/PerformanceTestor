"""响应变量提取：移植自 ResponseVarExtractor.java。

提取失败静默跳过（不中断压测，只影响后续步骤的占位值）；
不引入 jsonpath 依赖：点分路径已覆盖登录态提取这一核心场景。
"""

from __future__ import annotations

from typing import Any

from scenario.model import VarExtractSpec
from stress.response_access import header_value, parse_json, resolve_path, value_to_text
from stress.template_renderer import render as _render


def extract(
    body: str | None,
    headers: dict[str, list[str]] | None,
    rules: list[VarExtractSpec] | None,
    pool: dict[str, str],
    max_body_chars: int,
) -> None:
    """按提取规则从响应取值并写入变量池（body JSON 点分路径 / 响应头）。

    headers 形如 {头名: [值]}（多值头取第一个）；body 参与解析的最大字符数
    由 max_body_chars 限制（超长响应截断，避免压测机被大响应拖垮）。
    """
    if not rules:
        return
    root: Any = None
    root_resolved = False
    for rule in rules:
        if not rule.var or not rule.path:
            continue
        source = rule.source or "body"
        if source.lower() == "header":
            v = header_value(headers, rule.path)
            if v is not None:
                pool[rule.var] = v
            continue
        if not body or not body.strip():
            continue
        if not root_resolved:
            root = parse_json(body, max_body_chars)
            root_resolved = True
        if root is None:
            continue
        node = resolve_path(root, rule.path)
        if node is None:
            continue
        pool[rule.var] = value_to_text(node)


def create_pool(initial: dict[str, str] | None) -> dict[str, str]:
    """创建任务级变量池（用请求中的初始变量填充）。"""
    return dict(initial) if initial else {}


def substitute(template: str | None, pool: dict[str, str] | None) -> str:
    """替换模板中的 {{var}}（历史兼容入口，等价于无上下文池的双池渲染）。

    变量不存在时保留原占位符（便于排查配置错误，而不是静默变成空串）。
    """
    return _render(template, pool, None, None)


def describe(pool: dict[str, str]) -> str:
    """日志脱敏：仅记录变量名与值长度，不打印凭据内容。"""
    if not pool:
        return "{}"
    parts = [f"{k}=len({len(v) if v is not None else 0})" for k, v in pool.items()]
    return "{" + ", ".join(parts) + "}"
