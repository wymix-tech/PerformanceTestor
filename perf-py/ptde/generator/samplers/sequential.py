"""Sequential 采样器（总方案 §7.1：连续编号或有序键）。

参数：seq_start/start（缺省 1）、seq_prefix/prefix（缺省 ""）、step（缺省 1）。
step=1 时与 primitive.seq_value / legacy SeqRule 语义逐值一致（口径对齐）。
row_index 由 context["row_index"] 提供（缺省 0）。
"""

from __future__ import annotations

import random

from ptde.models.profile import DistributionFit


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    p = fit.params or {}
    row_index = int((context or {}).get("row_index", 0) or 0)
    start = p.get("seq_start", p.get("start"))
    start = int(start) if start is not None else 1
    prefix = p.get("seq_prefix", p.get("prefix"))
    prefix = prefix if prefix is not None else ""
    step = int(p.get("step", 1) or 1)
    return str(prefix) + str(start + row_index * step)
