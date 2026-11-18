"""NullRatio 采样器（总方案 §7.1：空值比例）。

参数：null_ratio（空值概率）、base（子分布 dict：type+params，非空时递归
分发采样）。null_ratio 不在 params 时显式失败；base 缺失时非空分支显式失败
（不得静默给默认值）。
"""

from __future__ import annotations

import random

from ptde.models.profile import DistributionFit


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    ratio = p.get("null_ratio")
    if ratio is None:
        raise ValueError(f"null_ratio 分布需要 null_ratio 参数，得到 {sorted(p)}")
    if rng.random() < float(ratio):
        return None
    base = p.get("base")
    if not isinstance(base, dict) or "type" not in base:
        raise ValueError(
            "null_ratio 分布需要 base 子分布参数（type+params 形态，显式失败不伪装）"
        )
    sub = DistributionFit(**base)
    from ptde.generator.samplers import sample as dispatch

    return dispatch(sub, rng, context)
