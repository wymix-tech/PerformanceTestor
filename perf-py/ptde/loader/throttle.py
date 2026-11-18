"""并发与限流（R5 T5.8，实施方案 §8.2；总方案 §9.5）。

口径（勿另起一套）：
- **并发度与批次大小必须经 Pilot Run 校准**（无通用固定最优值）——
  未经校准显式配置固定并发/批次 → 拒绝（显式失败，不伪装已校准）；
- 监控指标：数据库 CPU、IO、事务日志增长、锁等待（软限流——降档不中断，
  告警可见不吞）；生成端 CPU/内存/暂存空间由 R4 侧预算口径承载；
- 校准来源：``GenerationPilotReport``（concurrency ← workers；batch ← 装载
  通道实测批次；吞吐 ← rows/load_ms 供 T5.5 LoadBudget 复用，单一来源）。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from ptde_common.model_base import VersionedModel

__all__ = [
    "DEFAULT_LOG_CAPACITY_BYTES",
    "SOFT_LIMIT_METRICS",
    "ThrottleConfig",
    "ThrottleGuard",
    "ThrottleSignal",
    "calibrate_from_pilot",
]

# 软限流观测指标键（§9.5 控制指标子集——数据库侧；键集锁死防漂移）
SOFT_LIMIT_METRICS: tuple[str, ...] = (
    "database_cpu",      # 数据库 CPU 百分比（0-100）
    "io_util",           # IO 利用率百分比（0-100）
    "log_growth_mb",     # 事务日志增长（MB）
    "lock_waits",        # 锁等待（个/窗口）
)

DEFAULT_LOG_CAPACITY_BYTES = 32 * 1024**3  # 观测参考值（非并发度固定值；§9.5）


class ThrottleSignal(VersionedModel):
    """软限流信号（降档建议可见不吞；不中断执行——软限流语义）。"""

    metric: str
    observed: float
    soft_limit: float
    action: str  # 降档建议（如 reduce_concurrency / widen_interval）

    def message(self) -> str:
        return (
            f"软限流触发：{self.metric}={self.observed} > 软限 {self.soft_limit}"
            f"（建议：{self.action}）"
        )


class ThrottleConfig(VersionedModel):
    """限流配置（并发/批次必须携带 Pilot 校准来源）。"""

    concurrency: int | None = None  # None = 未校准（禁止使用通用固定值）
    batch_rows: int | None = None
    max_connections: int | None = None
    soft_limits: dict[str, float] = Field(default_factory=dict)  # SOFT_LIMIT_METRICS 子集
    calibrated: bool = False
    source: str | None = None  # 校准来源（如 pilot:entity）

    def model_post_init(self, __context: Any) -> None:
        unknown = set(self.soft_limits) - set(SOFT_LIMIT_METRICS)
        if unknown:
            raise ValueError(
                f"软限流指标键不在观测全集 {SOFT_LIMIT_METRICS}：{sorted(unknown)}"
                "（键集锁死防漂移，显式失败）"
            )
        if not self.calibrated and (self.concurrency is not None
                                    or self.batch_rows is not None):
            raise ValueError(
                "并发度/批次必须经 Pilot Run 校准后显式配置"
                "（总方案 §9.5：无通用固定最优值；固定值需 calibrated=True "
                "并注明 source，不得伪装校准）"
            )

    def validate_metrics(self, metrics: Mapping[str, float]) -> list[str]:
        """缺失观测指标的键（软限流观测口径：记录告警，不静默视为无风险）。"""
        return [k for k in self.soft_limits if k not in metrics]


def calibrate_from_pilot(report, batch_rows: int) -> ThrottleConfig:
    """由 GenerationPilotReport 校准（A2 实测口径；无实测装载 → 显式失败）。"""
    if not report.rows_loaded:
        raise ValueError(
            "Pilot 报告无实测装载行数（干跑不可校准限流参数，显式失败——"
            "总方案 §9.5：并发度/批次经 Pilot Run 校准，无通用固定值）"
        )
    return ThrottleConfig(
        concurrency=report.workers,
        batch_rows=batch_rows,
        calibrated=True,
        source=f"pilot:{report.entity}",
    )


class ThrottleGuard:
    """动态并发闸（校准并发 + 软限流降档；线程安全、确定性）。"""

    def __init__(self, config: ThrottleConfig) -> None:
        if not config.calibrated or config.concurrency is None:
            raise ValueError(
                "ThrottleGuard 需要经 Pilot Run 校准的并发度（无通用固定值，"
                "总方案 §9.5——显式失败，不默认并发）"
            )
        self._config = config
        self._limit = config.concurrency
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(config.concurrency)
        self._in_flight = 0
        self._signals: list[ThrottleSignal] = []

    @property
    def limit(self) -> int:
        with self._lock:
            return self._limit

    @property
    def signals(self) -> list[ThrottleSignal]:
        """触发过的软限流信号（可见不吞）。"""
        with self._lock:
            return list(self._signals)

    def acquire(self) -> bool:
        """获取并发槽：已达当前 limit → False（软限流退避；不无限排队）。"""
        with self._lock:
            if self._in_flight >= self._limit:
                return False
            self._in_flight += 1
        self._slots.acquire()
        return True

    def release(self) -> None:
        self._slots.release()
        with self._lock:
            self._in_flight -= 1

    def observe(self, metrics: Mapping[str, float]) -> list[ThrottleSignal]:
        """软限流观测：超限 → 降档（并发减半，下限 1）并记录信号；不中断。

        运行中的连接自然回落；降档后新增并发由 acquire 的 limit 把关。
        缺失已配置的观测指标 → 记入信号清单外的告警（不静默视为无风险）。
        """
        missing = self._config.validate_metrics(metrics)
        triggered: list[ThrottleSignal] = []
        for key, soft_limit in self._config.soft_limits.items():
            if key in missing:
                continue
            observed = float(metrics[key])
            if observed <= soft_limit:
                continue
            triggered.append(ThrottleSignal(
                metric=key,
                observed=observed,
                soft_limit=soft_limit,
                action=self._action_for(key),
            ))
        with self._lock:
            self._signals.extend(triggered)
            if triggered:
                self._limit = max(1, self._limit // 2)
        return triggered

    def _action_for(self, metric: str) -> str:
        if metric in ("database_cpu", "io_util"):
            return "reduce_concurrency（并发减半，下限 1）"
        if metric == "log_growth_mb":
            return "widen_interval（增大批次间隔/降低批量，控制日志增长）"
        if metric == "lock_waits":
            return "reduce_concurrency_or_rebatch（降并发或降低批量缓解行锁冲突）"
        return "back_off"
