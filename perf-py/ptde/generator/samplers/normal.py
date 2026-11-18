"""Normal 采样器（总方案 §7.1：正态或近似正态数值）。

口径与 derivation.sample_one 一致：max(0, gauss(mean, sigma))——关联基数等
数值列非负语义；params.min/max 存在时进一步钳制。
"""

from __future__ import annotations

import random

from ptde.models.profile import DistributionFit


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    if p.get("mean") is None:
        raise ValueError(f"normal 分布需要 mean 参数，得到 {sorted(p)}")
    sigma = float(p.get("sigma", p.get("std", 0.0)) or 0.0)
    value = max(0.0, rng.gauss(float(p["mean"]), sigma))
    if p.get("min") is not None:
        value = max(value, float(p["min"]))
    if p.get("max") is not None:
        value = min(value, float(p["max"]))
    return value
