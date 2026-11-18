"""WeightedCategorical 采样器（总方案 §7.1：状态、类型和类别比例）。

参数键名与 B3 产出严格一致：
- values/weights 列表形态（distribution_planner._stats_fit / derivation）——主形态；
- {bucket: ratio} 字典形态（B2 collector 直方图形态）——兼容形态。
累计分布 + pick <= acc（口径对齐 derivation.sample_one）；零权重值不会被选中。
"""

from __future__ import annotations

import random

from ptde.models.profile import DistributionFit

_RESERVED_KEYS = ("values", "weights")


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    values, weights = p.get("values"), p.get("weights")
    if isinstance(values, list) and isinstance(weights, list) \
            and len(values) == len(weights) and weights:
        total = sum(float(w) for w in weights)
        pick = rng.random() * total
        acc = 0.0
        for v, w in zip(values, weights, strict=True):
            acc += float(w)
            if pick <= acc:
                return v
        return values[-1]
    # 兼容 B2 collector 的 {bucket: ratio} 字典形态
    items = [(k, float(v)) for k, v in p.items()
             if k not in _RESERVED_KEYS and isinstance(v, (int, float))
             and not isinstance(v, bool) and v > 0]
    if items:
        total = sum(ratio for _, ratio in items)
        pick = rng.random() * total
        acc = 0.0
        for bucket, ratio in items:
            acc += ratio
            if pick <= acc:
                return bucket
        return items[-1][0]
    raise ValueError(
        "weighted_categorical 需要 values/weights 列表或 {bucket: ratio} 字典参数，"
        f"得到 {sorted(p)}"
    )
