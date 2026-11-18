"""Random 采样器（总方案 §7.1：随机离散值）。

参数：values（离散值集合，均匀随机取一）。缺 values 显式失败（不静默退化）。
"""

from __future__ import annotations

import random

from ptde.models.profile import DistributionFit


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    values = p.get("values")
    if isinstance(values, list) and values:
        return values[rng.randrange(len(values))]
    raise ValueError(f"random 分布需要 values（离散值集合）参数，得到 {sorted(p)}")
