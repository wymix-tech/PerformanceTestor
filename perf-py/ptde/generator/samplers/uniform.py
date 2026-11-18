"""Uniform 采样器（总方案 §7.1：均匀分布）。

参数（B3 distribution_planner 产出形态）：min/max；precision 仅供 R4.2 定标
参考，采样层不做额外截断（与 derivation.sample_one 口径一致，勿另起一套）。
"""

from __future__ import annotations

import random

from ptde.models.profile import DistributionFit


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    """[min, max] 均匀采样：整数域 randint、连续域 uniform（口径对齐 derivation）。"""
    p = fit.params or {}
    if p.get("min") is None or p.get("max") is None:
        raise ValueError(f"uniform 分布需要 min/max 参数，得到 {sorted(p)}")
    lo, hi = float(p["min"]), float(p["max"])
    if lo > hi:
        lo, hi = hi, lo
    if lo == int(lo) and hi == int(hi):
        return rng.randint(int(lo), int(hi))
    return rng.uniform(lo, hi)
