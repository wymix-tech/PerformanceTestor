"""压测统计器：语义逐位对齐 StressStatistician（Java 侧）。

为什么不用 hdrhistogram 库：Java 侧 StressStatistician 是平坦 30000 桶 × 1ms
直方图（桶下标即毫秒值），而 hdrhistogram 是对数分桶（误差线性放大），
两者分位数不可精确对拍。为满足阶段 2 验收「P95/P99 偏差 <10%」乃至逐位一致，
这里用同样的平坦数组实现：

- 桶宽 1ms，覆盖 [0, 30000ms)，超出/负值钳位进首尾桶（record 的 min/max 语义一致）
- percentile(hist, p)：累计计数 >= total*p/100 时返回桶下标（Java 同款算法，
  含 "向后取桶" 的取整行为——两侧同输入必同输出）
- max(hist)：最后一个非零桶下标
- collect_window()：累计直方图差值，各采样周期独立（对齐 Java 窗口语义）

线程安全：record 用锁（Python GIL 下数组自增本就原子，锁只为保证
collect_window 基线更新与 record 的有序性；高并发下该锁竞争面远小于发压本身，
真正的发压并发由 Locust/gevent 承担，这里只做聚合）。
"""

from __future__ import annotations

import math
import threading

BUCKET_COUNT = 30000
BUCKET_COUNT_MS = BUCKET_COUNT  # 桶下标 == 毫秒值


def round2(v: float) -> float:
    """对齐 Java Math.round(v*100)/100.0：floor(x+0.5) 向正无穷取半。"""
    return math.floor(v * 100 + 0.5) / 100.0


def percentile(hist: list[int], p: float) -> int:
    """从直方图计算分位数（毫秒）。

    算法与 StressStatistician.percentile 逐行对齐：
    累计计数首次达到 total*p/100 的桶下标即为结果（整数毫秒）。
    """
    total = sum(hist)
    if total <= 0:
        return 0
    target = total * p / 100.0
    cum = 0
    for i, v in enumerate(hist):
        cum += v
        if cum >= target:
            return i
    return len(hist) - 1


def histogram_max(hist: list[int]) -> int:
    """最后一个非零桶下标（毫秒）；空直方图返回 0。"""
    for i in range(len(hist) - 1, -1, -1):
        if hist[i] > 0:
            return i
    return 0


class HistogramStatistician:
    """单实例统计器：计数 + 30000 桶直方图 + 窗口基线（对应 StressStatistician）。"""

    def __init__(self, bucket_count: int = BUCKET_COUNT) -> None:
        self._bucket_count = bucket_count
        self._hist = [0] * bucket_count
        self._last_hist = [0] * bucket_count
        self._lock = threading.Lock()
        self._total = 0
        self._errors = 0
        self._cost_ms = 0
        self._last_total = 0
        self._last_errors = 0
        self._last_cost_ms = 0

    def record(self, cost_ms: float, error: bool) -> None:
        """记录一次请求耗时（毫秒）与是否错误（对齐 record 的钳位语义）。"""
        idx = int(min(max(cost_ms, 0), self._bucket_count - 1))
        with self._lock:
            self._hist[idx] += 1
            self._total += 1
            self._cost_ms += cost_ms
            if error:
                self._errors += 1

    def collect_window(self) -> dict:
        """收集自上次调用以来的窗口统计（累计直方图差值）。"""
        with self._lock:
            window = [self._hist[i] - self._last_hist[i] for i in range(self._bucket_count)]
            count = self._total - self._last_total
            errors = self._errors - self._last_errors
            cost = self._cost_ms - self._last_cost_ms
            self._last_hist = self._hist[:]
            self._last_total = self._total
            self._last_errors = self._errors
            self._last_cost_ms = self._cost_ms
        return {
            "count": count,
            "error_count": errors,
            "total_cost_ms": cost,
            "histogram": window,
        }

    def stage_snapshot(self) -> dict:
        """当前累计快照（用于最终结果；对齐 stageSnapshot）。"""
        with self._lock:
            return {
                "total": self._total,
                "error_count": self._errors,
                "total_cost_ms": self._cost_ms,
                "histogram": self._hist[:],
            }

    # ------------------------------------------------------- result helpers
    def build_summary(self, formal_elapsed_sec: float) -> dict:
        """基于累计快照构造最终统计（对齐 HttpStressEngine StageOutcome 组装逻辑）。

        qps = total / formalElapsedSec（正式期时长，至少 1s）；
        errorRate 为百分数（round2）。
        """
        snap = self.stage_snapshot()
        total = snap["total"]
        errors = snap["error_count"]
        elapsed = max(1.0, formal_elapsed_sec)
        hist = snap["histogram"]
        return {
            "total": total,
            "errors": errors,
            "qps": round2(total / elapsed),
            "p50": percentile(hist, 50),
            "p95": percentile(hist, 95),
            "p99": percentile(hist, 99),
            "max": histogram_max(hist),
            "avg": round2(snap["total_cost_ms"] / total) if total > 0 else 0,
            "error_rate": 0 if total == 0 else round2(errors * 100.0 / total),
        }
