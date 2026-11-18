"""Zipf/Skewed 采样器（总方案 §7.1：热点实体和高度倾斜）。

参数（对齐 B2/B3 产出）：
- exponent / s / fitted_parameter（B2 collector 拟合形态）→ Zipf 指数；
- max_rank → 热点排名上限（缺省 1000，上限 100000）；
- values → 按 rank 映射到候选值（热点值在前）；或 min/max → rank 线性映射到
  数值区间；均缺省时返回 rank 整数。
params.mean 存在且无显式值域时直接返回 mean——期望口径与
derivation.distribution_expectation 一致（勿另起一套）。
"""

from __future__ import annotations

import bisect
import random
from functools import lru_cache

from ptde.models.profile import DistributionFit

MAX_RANK_LIMIT = 100_000


@lru_cache(maxsize=128)
def _cumulative(exponent: float, max_rank: int) -> tuple[float, ...]:
    """离散 Zipf 逆变换累积表：P(rank=k) ∝ 1/k^s。"""
    total = 0.0
    acc: list[float] = []
    for k in range(1, max_rank + 1):
        total += 1.0 / (k ** exponent)
        acc.append(total)
    return tuple(acc)


def sample_rank(rng: random.Random, exponent: float, max_rank: int) -> int:
    """按 Zipf(exponent) 采样 rank ∈ [1, max_rank]（逆 CDF + 二分）。"""
    table = _cumulative(exponent, max_rank)
    pick = rng.random() * table[-1]
    return bisect.bisect_left(table, pick) + 1


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    exponent = p.get("exponent", p.get("s", p.get("fitted_parameter")))
    s = float(exponent) if exponent is not None else 1.0
    max_rank = max(1, min(int(p.get("max_rank", 1000)), MAX_RANK_LIMIT))
    rank = sample_rank(rng, s, max_rank)
    values = p.get("values")
    if isinstance(values, list) and values:
        return values[min(rank - 1, len(values) - 1)]
    lo, hi = p.get("min"), p.get("max")
    if lo is not None and hi is not None:
        step = (float(hi) - float(lo)) / max(1, max_rank - 1)
        return float(lo) + (rank - 1) * step
    if p.get("mean") is not None:
        # 期望口径与 distribution_expectation 一致（zipf/skewed 取 params.mean）
        return float(p["mean"])
    return rank
