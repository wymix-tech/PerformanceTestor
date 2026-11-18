"""Conditional 采样器（总方案 §7.1：根据其他字段条件生成）。

参数键名与 B3 产出严格一致：depends_on（依赖字段列表，planner.py 折叠形态）。
branches：{依赖字段取值 → 分支分布}——分支值可为字面量、候选值列表或
DistributionFit dict（递归分发）；"*" 为兜底分支。
branches 缺失或依赖取值无匹配且无兜底时显式失败——不得静默退化为边缘分布
（A3：否则可能产生 status=PENDING 但 paid_at 已有值等不合理记录）。
依赖字段取值经 context 提供（R4.3 依赖图拓扑保证先生成）。
"""

from __future__ import annotations

import random

from ptde.models.profile import DistributionFit


def _resolve_branch(value, rng: random.Random, context: dict | None):
    """分支值三类形态：字面量 / 候选列表 / DistributionFit dict（递归分发）。"""
    if isinstance(value, dict) and "type" in value:
        fit = DistributionFit(**value)
        from ptde.generator.samplers import sample as dispatch

        return dispatch(fit, rng, context)
    if isinstance(value, list) and value:
        return value[rng.randrange(len(value))]
    return value


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    depends_on = p.get("depends_on") or []
    depends_on = depends_on if isinstance(depends_on, list) else [depends_on]
    branches = p.get("branches")
    if not isinstance(branches, dict) or not branches:
        raise ValueError(
            "conditional 分布缺少 branches 参数，无法按 P(value|depends_on) 采样"
            "（不得静默退化为边缘分布）"
        )
    ctx = context or {}
    if len(depends_on) == 1:
        key = ctx.get(depends_on[0])
    else:
        key = tuple(ctx.get(d) for d in depends_on)
    # key 存在时分支值可为 None 字面量（如 PENDING → 空值）；仅 key 缺失才兜底 "*"
    branch = branches[key] if key in branches else branches.get("*")
    if branch is None and key not in branches:
        raise ValueError(
            f"conditional 分布无匹配分支：depends_on={depends_on} 取值 {key!r}"
            "，且无 '*' 兜底分支（显式失败，不伪装）"
        )
    return _resolve_branch(branch, rng, context)
