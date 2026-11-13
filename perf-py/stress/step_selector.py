"""场景步骤加权选择器：移植自 StepSelector.java。

构造时预计算累积权重数组，选择用二分查找定位（单请求 O(log n)）。
无状态、不可变、线程安全（Python 侧选择用 random，无共享可变状态）。
"""

from __future__ import annotations

import random


class StepSelector:
    def __init__(self, weights: list[int]) -> None:
        """各步骤权重（<=0 视为 0，即永不被选中）。"""
        self._cumulative: list[int] = []
        total = 0
        for w in weights:
            total += max(0, w)
            self._cumulative.append(total)
        self._total_weight = total

    @classmethod
    def of(cls, steps: list) -> StepSelector:
        """从步骤列表构造（读取各步权重，缺省按 1）。"""
        return cls([max(1, getattr(s, "weight", 1)) for s in steps])

    def select(self) -> int:
        """随机选中一个步骤下标；总权重为 0 时返回 -1。"""
        if self._total_weight <= 0:
            return -1
        target = random.randint(1, self._total_weight)
        # 二分查找第一个累积权重 >= target 的下标
        lo, hi = 0, len(self._cumulative) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self._cumulative[mid] >= target:
                hi = mid
            else:
                lo = mid + 1
        return lo

    @property
    def total_weight(self) -> int:
        return self._total_weight

    def __len__(self) -> int:
        return len(self._cumulative)
