"""T5.8 并发与限流测试（实施方案 §8.2；总方案 §9.5）。

覆盖：并发/批次必须经 Pilot Run 校准（无通用固定值——未校准固定值显式
失败）、软限流观测（CPU/IO/日志增长/锁等待）与降档语义（降档不中断、
信号可见不吞）、闸并发上限确定性。
"""

from __future__ import annotations

import pytest

from ptde.loader.throttle import (
    SOFT_LIMIT_METRICS,
    ThrottleConfig,
    ThrottleGuard,
    calibrate_from_pilot,
)
from ptde.planner.pilot_run import GenerationPilotReport

# ============================================================ 校准语义（§9.5）


def test_uncalibrated_fixed_values_rejected() -> None:
    """无通用固定最优值：未校准即配固定并发/批次 → 显式失败。"""
    with pytest.raises(ValueError, match="Pilot Run 校准"):
        ThrottleConfig(concurrency=8, batch_rows=5000)


def test_calibrated_config_requires_source() -> None:
    cfg = ThrottleConfig(concurrency=4, batch_rows=2000,
                         calibrated=True, source="pilot:pilot_orders")
    assert cfg.concurrency == 4
    assert cfg.source == "pilot:pilot_orders"


def test_soft_limit_unknown_metric_key_rejected() -> None:
    with pytest.raises(ValueError, match="观测全集"):
        ThrottleConfig(calibrated=True, concurrency=2,
                       soft_limits={"network_mbps": 100})


def test_soft_limit_metric_keys_locked() -> None:
    assert SOFT_LIMIT_METRICS == (
        "database_cpu", "io_util", "log_growth_mb", "lock_waits",
    )


def test_validate_metrics_reports_missing_keys() -> None:
    cfg = ThrottleConfig(calibrated=True, concurrency=2,
                         soft_limits={"database_cpu": 75, "lock_waits": 10})
    assert cfg.validate_metrics({"database_cpu": 50}) == ["lock_waits"]
    assert cfg.validate_metrics({"database_cpu": 50, "lock_waits": 0,
                                 "io_util": 10}) == []


# ============================================================ Pilot Run 校准


def test_calibrate_from_pilot_uses_measured_workers() -> None:
    report = GenerationPilotReport(
        entity="pilot_orders", total_rows=1000, rows_generated=1000,
        rows_loaded=1000, shards=4, workers=4, load_ms=2000,
    )
    cfg = calibrate_from_pilot(report, batch_rows=500)
    assert cfg.calibrated is True
    assert cfg.concurrency == 4  # ← Pilot 实测 workers
    assert cfg.batch_rows == 500
    assert cfg.source == "pilot:pilot_orders"


def test_calibrate_from_pilot_dry_run_rejected() -> None:
    report = GenerationPilotReport(entity="pilot_orders")  # rows_loaded=None 干跑
    with pytest.raises(ValueError, match="不可校准"):
        calibrate_from_pilot(report, batch_rows=500)


# ============================================================ 并发闸


def test_guard_requires_calibrated_concurrency() -> None:
    with pytest.raises(ValueError, match="校准"):
        ThrottleGuard(ThrottleConfig())
    with pytest.raises(ValueError, match="校准"):
        ThrottleGuard(ThrottleConfig(concurrency=8))  # 未标 calibrated


def test_guard_acquire_release_roundtrip() -> None:
    guard = ThrottleGuard(ThrottleConfig(concurrency=1, calibrated=True,
                                         source="pilot:x"))
    assert guard.acquire() is True
    assert guard.acquire() is False  # 并发上限
    guard.release()
    assert guard.acquire() is True
    guard.release()


def test_guard_limit_two_blocks_third() -> None:
    guard = ThrottleGuard(ThrottleConfig(concurrency=2, calibrated=True,
                                         source="pilot:x"))
    assert guard.acquire() and guard.acquire()
    assert guard.acquire() is False
    assert guard.limit == 2
    guard.release()
    assert guard.acquire() is True


# ============================================================ 软限流（降档不中断）


def test_observe_over_cpu_soft_limit_degrades() -> None:
    cfg = ThrottleConfig(
        concurrency=4, calibrated=True, source="pilot:x",
        soft_limits={"database_cpu": 75},
    )
    guard = ThrottleGuard(cfg)
    assert guard.acquire() and guard.acquire()
    signals = guard.observe({"database_cpu": 90})
    assert len(signals) == 1
    assert signals[0].metric == "database_cpu"
    assert "reduce_concurrency" in signals[0].action
    assert signals[0].message().startswith("软限流触发")
    assert guard.limit == 2  # 并发减半（下限 1）
    assert guard.signals == signals  # 信号可见不吞
    # in_flight=2 ≥ 降档后 limit → 新并发被退避
    assert guard.acquire() is False
    guard.release()
    assert guard.acquire() is True


def test_observe_within_limit_no_signal() -> None:
    cfg = ThrottleConfig(
        concurrency=4, calibrated=True, source="pilot:x",
        soft_limits={"database_cpu": 75, "lock_waits": 10},
    )
    guard = ThrottleGuard(cfg)
    assert guard.observe({"database_cpu": 50, "lock_waits": 3}) == []
    assert guard.limit == 4


def test_observe_lock_waits_action() -> None:
    cfg = ThrottleConfig(
        concurrency=2, calibrated=True, source="pilot:x",
        soft_limits={"lock_waits": 10},
    )
    guard = ThrottleGuard(cfg)
    signals = guard.observe({"lock_waits": 25})
    assert "行锁冲突" in signals[0].action


def test_repeated_degradation_floors_at_one() -> None:
    cfg = ThrottleConfig(
        concurrency=4, calibrated=True, source="pilot:x",
        soft_limits={"database_cpu": 75},
    )
    guard = ThrottleGuard(cfg)
    guard.observe({"database_cpu": 99})
    guard.observe({"database_cpu": 99})
    guard.observe({"database_cpu": 99})
    assert guard.limit == 1  # 下限 1（降档不中断）
    assert len(guard.signals) == 3
