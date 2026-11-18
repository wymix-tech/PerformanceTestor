"""LengthDistribution 采样器（总方案 §7.1：字符串和大字段长度分布）。

参数：min_length/max_length（B3 distribution_planner 字符列默认形态
{"min_length": 1, "max_length": 64}）；mean/avg 存在时按截断正态取整，
否则区间均匀。长度只描述规模特征，不含明文内容（脱敏边界延续 R2）。
"""

from __future__ import annotations

import random

from ptde.models.profile import DistributionFit


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    lo = int(p.get("min_length", p.get("min", 1)) or 1)
    hi = int(p.get("max_length", p.get("max", lo)) or lo)
    if hi < lo:
        lo, hi = hi, lo
    mean = p.get("mean", p.get("avg"))
    if mean is not None:
        span = (hi - lo) / 4.0 or 1.0
        length = int(round(rng.gauss(float(mean), span)))
        return max(lo, min(hi, length))
    return rng.randint(lo, hi)
