"""OverThreshold 采样器（B7 续批次 9：legacy FieldRule range+over_threshold 语义对齐）。

对齐 legacy 口径（datagen/rule_engine.py TYPE_RANGE + OverThreshold）：
- share% 的行取超阈值区间：(value, max]（op=">"，nextafter 开区间）或
  [value, max]（op=">="）；
- 其余行取 [min, threshold]（阈值 ≤ min 时回退 [min, max]，不截断）；
- min 缺省 0、max 缺省 100、share 缺省 30（legacy 侧缺省同口径）；
- max < min 时交换（legacy 反转纠正同口径）；
- 整数域参数（min/max/threshold_value 均为整数值）→ 结果按 Java Math.round
  取整（floor(x+0.5)，对齐 legacy scale_to_column_type INT/BIGINT）；
  连续域参数 → 保留浮点（与 uniform 采样器整数域/连续域路由口径一致；
  DECIMAL 2 位定标由列类型归一化层承接，采样层不做截断）。
"""

from __future__ import annotations

import math
import random

from ptde.models.profile import DistributionFit


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    mn = float(p.get("min", 0.0))
    mx = float(p.get("max", 100.0))
    if mx < mn:
        mn, mx = mx, mn  # legacy 反转纠正同口径
    tv = p.get("threshold_value", p.get("value"))
    if tv is None:
        raise ValueError(
            f"over_threshold 分布需要 threshold_value 参数（阈值，显式失败），得到 {sorted(p)}")
    share = float(p.get("share", 30.0))
    op = p.get("threshold_op", p.get("op", ">"))
    if op not in (">", ">="):
        raise ValueError(f"over_threshold 分布 op 仅支持 >/>=（显式失败），得到 {op!r}")
    if rng.random() * 100 < share:
        # 越过阈值区间：(value, max]（op=">"）或 [value, max]（op=">="）
        low = float(tv) if op == ">=" else math.nextafter(float(tv), math.inf)
        picked = low + rng.random() * max(0.0, mx - low)
    else:
        # 未越阈区间：[min, threshold]（阈值低于 min 时回退 [min, max]）
        high = float(tv) if float(tv) > mn else mx
        picked = mn + rng.random() * (high - mn)
    integer_domain = all(
        float(p.get(k, d)) == int(float(p.get(k, d)))
        for k, d in (("min", 0.0), ("max", 100.0), ("threshold_value", tv)))
    if integer_domain:
        return int(math.floor(picked + 0.5))  # Java Math.round（legacy INT/BIGINT 对齐）
    return picked
