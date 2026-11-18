"""Correlated 采样器（总方案 §7.1：多字段联合分布）。

本字段取值条件于关联字段（context 提供）：y|x ~ 正态条件分布
  E[y|x] = mean + rho * sigma * (x - partner_mean) / partner_sigma
  SD[y|x] = sigma * sqrt(1 - rho²)
参数：depends_on（关联字段，str 或 list 取首个）、correlation/rho、mean、
sigma、partner_mean（缺省 mean）、partner_sigma（缺省 sigma）；
可选 min/max 钳制。缺关联字段取值时显式失败。
"""

from __future__ import annotations

import math
import random

from ptde.models.profile import DistributionFit


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    depends_on = p.get("depends_on") or p.get("partner")
    if isinstance(depends_on, list):
        depends_on = depends_on[0] if depends_on else None
    if not depends_on:
        raise ValueError(f"correlated 分布需要 depends_on（关联字段）参数，得到 {sorted(p)}")
    ctx = context or {}
    if ctx.get(depends_on) is None:
        raise ValueError(
            f"correlated 分布需要 context 提供关联字段 {depends_on} 的取值"
            "（依赖图拓扑保证先生成）"
        )
    rho = max(-1.0, min(1.0, float(p.get("correlation", p.get("rho", 0.0)))))
    mean = float(p.get("mean", 0.0) or 0.0)
    sigma = float(p.get("sigma", 1.0) or 1.0)
    partner_mean = float(p.get("partner_mean", mean))
    partner_sigma = float(p.get("partner_sigma", sigma))
    x = float(ctx[depends_on])
    cond_mean = mean + rho * sigma * (x - partner_mean) / partner_sigma \
        if partner_sigma != 0 else mean
    cond_sigma = sigma * math.sqrt(max(0.0, 1.0 - rho * rho))
    value = rng.gauss(cond_mean, cond_sigma)
    if p.get("min") is not None:
        value = max(value, float(p["min"]))
    if p.get("max") is not None:
        value = min(value, float(p["max"]))
    return value
