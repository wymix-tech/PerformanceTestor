"""父表主键池：移植自 engine/IdPool.java。

插入完成后回查主键填充；支持均匀采样（ref）与热点倾斜采样（refSkew：
hotPercent% 的父行承接 hotShare% 的子行，累计权重 + 二分 O(log n)/行）。
池上限 50 万（超限分段采样截断）；自引用表在插行过程中动态追加已插入 ID。
"""

from __future__ import annotations

import math
import random
import threading
from bisect import bisect_left

MAX_POOL_SIZE = 500_000


class IdPool:
    # 池上限（对齐 Java 静态常量 IdPool.MAX_POOL_SIZE）
    MAX_POOL_SIZE = MAX_POOL_SIZE

    def __init__(self) -> None:
        self._ids: list[int] = []
        self._cumulative_weights: list[float] | None = None
        self._weighted = False
        # Java synchronized 方法可重入（sampleSkewed 内调 sampleUniform），用 RLock 对齐
        self._lock = threading.RLock()

    def add_all(self, new_ids: list[int]) -> None:
        with self._lock:
            remain = MAX_POOL_SIZE - len(self._ids)
            if remain <= 0:
                return
            if len(new_ids) > remain:
                new_ids = new_ids[:remain]
            self._ids.extend(new_ids)
            self._weighted = False  # 池变更后需重建权重

    def add(self, id_: int) -> None:
        with self._lock:
            if len(self._ids) < MAX_POOL_SIZE:
                self._ids.append(id_)
                self._weighted = False

    def size(self) -> int:
        with self._lock:
            return len(self._ids)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._ids

    def min(self) -> int | None:
        with self._lock:
            return self._ids[0] if self._ids else None

    def max(self) -> int | None:
        with self._lock:
            return self._ids[-1] if self._ids else None

    def sample_uniform(self) -> int:
        """均匀采样一个父 ID。"""
        with self._lock:
            if not self._ids:
                raise RuntimeError("父 ID 池为空，无法采样 ref 值")
            return self._ids[random.randrange(len(self._ids))]

    def build_skew_weights(self, hot_percent: int, hot_share: int) -> None:
        """构建热点倾斜权重：前 hotCount 行合计权重 hotShare，其余合计 (100 - hotShare)。"""
        with self._lock:
            if not self._ids or hot_percent <= 0 or hot_percent >= 100:
                self._cumulative_weights = None
                self._weighted = False
                return
            hot_count = max(1, math.floor(len(self._ids) * hot_percent / 100.0 + 0.5))
            hot_each = hot_share / hot_count
            cold_each = (100 - hot_share) / max(1, len(self._ids) - hot_count)
            acc = 0.0
            w: list[float] = []
            for i in range(len(self._ids)):
                acc += hot_each if i < hot_count else cold_each
                w.append(acc)
            self._cumulative_weights = w
            self._weighted = True

    def sample_skewed(self) -> int:
        """热点倾斜采样：累计权重 + 二分查找 O(log n)/行。"""
        with self._lock:
            if not self._ids:
                raise RuntimeError("父 ID 池为空，无法采样 refSkew 值")
            if not self._weighted or self._cumulative_weights is None:
                return self.sample_uniform()
            cum = self._cumulative_weights
            pick = random.random() * cum[-1]
            # 第一个累计权重 >= pick 的下标
            return self._ids[bisect_left(cum, pick)]

    def sample_invalid_id(self) -> int:
        """无效 ID（orphanRate 替换用）：池最大值 + 1 + 随机偏移，必然不命中真实行。"""
        with self._lock:
            base = self._ids[-1] if self._ids else 1
            return base + 1 + random.randrange(1000)
