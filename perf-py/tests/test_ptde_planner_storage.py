"""T3.4 验收：存储估算（B_i 试装载实测校准，禁止字段长度相加）+ Pilot Run 校准协议。

不连真库：连接工厂注入 + FakeConn/FakeProbe 鸭子类型假连接（沿用 ptde 测试口径）。
"""

from __future__ import annotations

import random

import pytest

from ptde.planner.pilot_run import (
    PilotRunConfig,
    PilotRunReport,
    run_pilot,
)
from ptde.planner.storage_estimator import (
    CalibratedRowCost,
    estimate_storage,
)

GB = 1024**3


# ---------------------------------------------------------------- 估算器


def test_estimate_storage_with_calibration():
    """B_i 为实测校准值：表/索引成本分开累计（非字段长度相加）。"""
    costs = {
        "orders": CalibratedRowCost(table="orders", bytes_per_row=300.0,
                                    index_bytes_per_row=100.0, sample_rows=10_000,
                                    error_band="±3.0%（两轮扩样校准）"),
        "users": CalibratedRowCost(table="users", bytes_per_row=200.0,
                                   index_bytes_per_row=50.0, sample_rows=10_000,
                                   error_band="±3.0%（两轮扩样校准）"),
    }
    est, warnings = estimate_storage(
        {"orders": 1_000_000, "users": 500_000}, costs, include_indexes=True)
    assert est.estimated_table_size_bytes == 1_000_000 * 300 + 500_000 * 200
    assert est.estimated_index_size_bytes == 1_000_000 * 100 + 500_000 * 50
    assert est.estimated_total_size_bytes == est.estimated_table_size_bytes \
        + est.estimated_index_size_bytes
    assert est.calibrated is True and est.confidence == 0.9
    assert "扩样校准" in est.error_band
    assert warnings == []


def test_estimate_storage_uncalibrated_flagged():
    est, warnings = estimate_storage({"orders": 1_000}, None)
    assert est.calibrated is False and est.confidence == 0.3
    assert any("无 Pilot Run 校准记录" in w and "禁止作为容量承诺" in w for w in warnings)
    assert "未经试装载校准" in est.error_band


def test_estimate_storage_partial_calibration():
    costs = {"orders": CalibratedRowCost(table="orders", bytes_per_row=300.0,
                                         index_bytes_per_row=100.0, sample_rows=1)}
    est, warnings = estimate_storage({"orders": 1_000_000, "users": 1_000}, costs)
    assert est.calibrated is False and est.confidence == 0.5
    assert any("users" in w for w in warnings)


def test_estimate_storage_exclude_indexes():
    costs = {"orders": CalibratedRowCost(table="orders", bytes_per_row=300.0,
                                         index_bytes_per_row=100.0, sample_rows=1)}
    est, _ = estimate_storage({"orders": 1000}, costs, include_indexes=False)
    assert est.estimated_index_size_bytes is None
    assert est.estimated_total_size_bytes == 300_000


def test_calibrated_row_cost_validation():
    with pytest.raises(ValueError, match="必须为正"):
        CalibratedRowCost(table="t", bytes_per_row=0.0)


# ---------------------------------------------------------------- Pilot Run


class FakeCursor:
    """鸭子类型假游标：记录 executemany，无真库交互。"""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, *a):
        self.conn.executed_sql.append(sql)

    def executemany(self, sql, rows):
        self.conn.executemany_calls.append((sql, len(rows)))
        self.conn.inserted_rows += len(rows)

    def fetchone(self):
        return (0,)

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.executemany_calls = []
        self.inserted_rows = 0
        self.executed_sql = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


class FakeProbe:
    """模拟物理存储度量：行均表 300B + 索引 100B（含页填充非线性项）。"""

    def __init__(self, table_bytes_per_row=300.0, index_bytes_per_row=100.0):
        self.t = table_bytes_per_row
        self.i = index_bytes_per_row

    def measure(self, conn, table, include_indexes=True):
        rows = conn.inserted_rows
        table_bytes = int(rows * self.t + (1024 if rows else 0))
        index_bytes = int(rows * self.i) if include_indexes else 0
        return table_bytes, index_bytes, rows


def _run(sample_rows=1_000, multiplier=10):
    cfg = PilotRunConfig(sample_rows=sample_rows, scale_multiplier=multiplier,
                         batch_size=500)
    columns = [("id", "bigint"), ("user_id", "bigint"), ("status", "varchar(32)")]
    conn = FakeConn()
    report = run_pilot(
        "orders", columns, lambda: conn, config=cfg,
        probe=FakeProbe(), rng=random.Random(0),
    )
    return report, conn


def test_pilot_run_two_round_protocol():
    """协议：默认样本 → 扩样 10×；B_i 实测（表/索引成本分开归因，非列长度求和）。"""
    report, conn = _run()
    assert [r.rows_loaded for r in report.rounds] == [1_000, 11_000]
    # 每行成本 = 300（表）+ 100（索引）——来自探针模拟物理度量，而非字段长度相加
    assert report.bytes_per_row == pytest.approx(300.0)
    assert report.index_bytes_per_row == pytest.approx(100.0)
    assert report.rounds[-1].bytes_per_row == pytest.approx(400.0)


def test_pilot_run_batch_channel_and_sql():
    """R3 降级通道 DbApiBatchExecutor：批量 INSERT 批次 = ceil(行数/500)。"""
    report, conn = _run()
    assert report.channel == "dbapi_batch"
    assert len(conn.executemany_calls) == 2 + 20  # 1000/500 + 10000/500
    sql, n = conn.executemany_calls[0]
    assert sql.startswith("INSERT INTO orders (id, user_id, status)")
    assert n == 500
    assert report.rows_verified is True, "探针行数增量与装载行数一致（数量复核锚点）"


def test_pilot_run_error_band_from_two_rounds():
    """扩样 10× 修正非线性偏差：误差区间取两轮行均成本偏差。"""
    # 恒定行成本 → 两轮一致（基线扣页开销后）→ 误差区间 < 1%
    probe = FakeProbe(table_bytes_per_row=350.0)
    cfg = PilotRunConfig(sample_rows=1000, scale_multiplier=10, batch_size=500)
    conn = FakeConn()
    report = run_pilot("orders", [("id", "bigint")], lambda: conn, config=cfg,
                       probe=probe, rng=random.Random(0))
    assert report.error_band.startswith("±0.")
    assert "两轮扩样校准" in report.error_band


def test_pilot_run_nonlinear_deviation_band():
    """模拟非线性偏差：小样本页填充稀疏（350B/行），扩样收敛（320B/行）。"""
    class NonlinearProbe:
        def measure(self, conn, table, include_indexes=True):
            rows = conn.inserted_rows
            rate = 350.0 if rows <= 1000 else 320.0
            return int(rows * rate), int(rows * 100), rows

    cfg = PilotRunConfig(sample_rows=1000, scale_multiplier=10, batch_size=500)
    conn = FakeConn()
    report = run_pilot("orders", [("id", "bigint")], lambda: conn, config=cfg,
                       probe=NonlinearProbe(), rng=random.Random(0))
    # round1 行均 450（350+100），round2 行均 417（增量 10000 行×320B 表 + 索引 100B）
    assert report.rounds[0].bytes_per_row == pytest.approx(450.0)
    assert report.rounds[-1].bytes_per_row == pytest.approx(417.0)
    assert report.error_band.startswith("±7")
    # B_i 取大样本轮次增量（更收敛的页填充）；索引成本分项归因
    assert report.bytes_per_row == pytest.approx(317.0)
    assert report.index_bytes_per_row == pytest.approx(100.0)


def test_pilot_run_report_serializable_and_calibration():
    """报告可序列化落库（VersionedModel：seal + JSON 往返）并可转校准记录。"""
    report, _ = _run()
    assert report.fingerprint and len(report.fingerprint) == 64
    restored = PilotRunReport.from_json(report.to_json())
    assert restored == report
    assert restored.compute_fingerprint() == report.fingerprint
    cost = report.to_calibration()
    assert cost.table == "orders"
    assert cost.bytes_per_row == pytest.approx(300.0)
    assert cost.index_bytes_per_row == pytest.approx(100.0)
    assert cost.calibrated is True


def test_pilot_run_config_validation():
    with pytest.raises(ValueError, match="sample_rows"):
        PilotRunConfig(sample_rows=0)
    with pytest.raises(ValueError, match="scale_multiplier"):
        PilotRunConfig(scale_multiplier=1)
    with pytest.raises(ValueError, match="batch_size"):
        PilotRunConfig(batch_size=-1)


def test_pilot_run_calibration_feeds_estimator():
    """端到端：Pilot Run 报告 → CalibratedRowCost → estimate_storage 校准生效。"""
    report, _ = _run()
    cost = report.to_calibration()
    est, warnings = estimate_storage({"orders": 1_000_000}, {"orders": cost})
    assert est.calibrated is True and not warnings
    # 1e6 行 × (300 表 + 100 索引) = 4e8 字节（实测 B_i，非字段长度相加）
    assert est.estimated_total_size_bytes == pytest.approx(400_000_000)
