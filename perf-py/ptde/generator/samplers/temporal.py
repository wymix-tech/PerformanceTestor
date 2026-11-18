"""Temporal 采样器（总方案 §7.1：时间序列、冷热数据和业务高峰）。

参数形态：
- buckets：B2 collector 相对时间分桶占比
  {last_7_days/last_30_days/last_90_days/older: ratio}——按占比选桶、桶内
  窗口均匀取回看天数（窗口口径与 collector._temporal_buckets 一致）；
- start/end：ISO 区间内均匀采样。
now 经 context["now"] 注入（固定 time_inputs 才可复现，验收门禁 1）；
缺省取当前 UTC 时间（不可复现路径，由 manifest.time_inputs 固定）。
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from ptde.models.profile import DistributionFit

# 桶名 → 回看天数窗口 [lo, hi]（与 collector._temporal_buckets 成员判定一致）
BUCKET_WINDOWS = {
    "last_7_days": (0, 7),
    "last_30_days": (8, 30),
    "last_90_days": (31, 90),
    "older": (91, 365),
}


def _weighted_bucket(rng: random.Random, buckets: dict[str, float]) -> str:
    items = [(name, float(ratio)) for name, ratio in buckets.items() if ratio > 0]
    if not items:
        raise ValueError("temporal buckets 占比均为非正数，无法采样")
    total = sum(ratio for _, ratio in items)
    pick = rng.random() * total
    acc = 0.0
    for name, ratio in items:
        acc += ratio
        if pick <= acc:
            return name
    return items[-1][0]


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    buckets = p.get("buckets")
    now = (context or {}).get("now")
    if now is None:
        now = datetime.now(UTC)
    if isinstance(buckets, dict) and buckets:
        name = _weighted_bucket(rng, {str(k): v for k, v in buckets.items()})
        if name not in BUCKET_WINDOWS:
            raise ValueError(f"temporal buckets 含未知桶名 {name!r}"
                             f"（支持：{sorted(BUCKET_WINDOWS)}）")
        lo, hi = BUCKET_WINDOWS[name]
        days = rng.randint(lo, hi)
        return now - timedelta(days=days)
    start, end = p.get("start"), p.get("end")
    if start is not None and end is not None:
        lo = datetime.fromisoformat(str(start).replace("T", " "))
        hi = datetime.fromisoformat(str(end).replace("T", " "))
        if lo > hi:
            lo, hi = hi, lo
        span = (hi - lo).total_seconds()
        return lo + timedelta(seconds=rng.random() * span)
    raise ValueError("temporal 分布需要 buckets 或 start/end 参数")
