"""LogNormal 采样器（总方案 §7.1：长尾数值）。

口径与 derivation.sample_one / distribution_expectation 一致：
exp(gauss(mu, sigma))，期望 exp(mu + sigma²/2)。
"""

from __future__ import annotations

import math
import random

from ptde.models.profile import DistributionFit


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    if p.get("mu") is None:
        raise ValueError(f"lognormal 分布需要 mu/sigma 参数，得到 {sorted(p)}")
    sigma = float(p.get("sigma", 0.0) or 0.0)
    value = math.exp(rng.gauss(float(p["mu"]), sigma))
    if p.get("min") is not None:
        value = max(value, float(p["min"]))
    if p.get("max") is not None:
        value = min(value, float(p["max"]))
    return value
