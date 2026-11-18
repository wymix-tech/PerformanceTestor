"""T4.12 验收：A2 生成 Pilot（实施方案 §7.2；门禁 3 内存可控 / 门禁 4 Pilot Run）。

- 端到端编排：分片规划 → 多进程生成 → staging → DbApiBatchExecutor 降级装载；
- 数量复核锚点：rows_loaded == rows_generated，staging 终态 verify_complete；
- A2 门禁判定：时长 + 峰值内存双达标；不达标 → A2 决策反转评估 note；
- conn_factory=None 干跑路径显式说明（不伪装已装载）；
- 可复现：同 root_seed 两次 Pilot 的 staging 校验和一致（门禁 1 分片级）。
"""

from __future__ import annotations

import os

import pytest

from ptde.planner.pilot_run import (
    A2_PILOT_BUDGET_MINUTES,
    DEFAULT_PILOT_MEMORY_BUDGET,
    GenerationPilotConfig,
    run_generation_pilot,
)


class FakeCursor:
    """鸭子类型假游标：记录 executemany，无真库交互（沿用 planner 测试口径）。"""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, *a):
        self.conn.executed_sql.append(sql)

    def executemany(self, sql, rows):
        self.conn.inserted_rows += len(rows)

    def fetchone(self):
        return (0,)

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.inserted_rows = 0
        self.executed_sql = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


def _config(tmp_path, total=5000, shard=1000, workers=4, seed=42) -> GenerationPilotConfig:
    return GenerationPilotConfig(
        entity="pilot_orders", total_rows=total, rows_per_shard=shard,
        workers=workers, batch_size=200, root_seed=seed,
        staging_dir=str(tmp_path / "staging"),
    )


# ---------------------------------------------------------------- 端到端（小规模实测）


def test_generation_pilot_end_to_end_with_fake_conn(tmp_path):
    cfg = _config(tmp_path, total=5000, shard=1000, workers=4)
    report = run_generation_pilot(FakeConn, cfg)
    assert report.entity == "pilot_orders"
    assert report.shards == 5  # ceil(5000/1000)
    assert report.rows_generated == 5000
    assert report.rows_loaded == 5000
    assert report.rows_verified  # 数量复核锚点
    assert report.duration_ms == report.generation_ms + report.load_ms
    assert report.within_budget  # 小规模实测应在预算内（门禁 3+4 双达标）
    assert report.channel == "dbapi_batch"
    assert report.peak_memory_bytes <= DEFAULT_PILOT_MEMORY_BUDGET


def test_generation_pilot_reproducible_across_runs(tmp_path):
    """同 DatasetSpec + Seed 两次 Pilot：staging 分片校验和一致（门禁 1 分片级）。"""
    r1 = run_generation_pilot(None, _config(tmp_path, seed=7))
    r2 = run_generation_pilot(
        None, _config(tmp_path / "second", seed=7))
    assert r1.rows_generated == r2.rows_generated == 5000
    from ptde.generator.staging import StagingArea

    s1 = StagingArea(tmp_path / "staging").restore()
    s2 = StagingArea(tmp_path / "second" / "staging").restore()
    c1 = sorted((x.shard_id, x.checksum) for x in s1.records())
    c2 = sorted((x.shard_id, x.checksum) for x in s2.records())
    assert c1 == c2  # 分片级逐片一致


def test_generation_pilot_dry_run_explicit_notes(tmp_path):
    report = run_generation_pilot(None, _config(tmp_path, total=500, shard=250))
    assert report.rows_generated == 500
    assert report.rows_loaded is None
    assert not report.rows_verified
    assert any("未装载" in n for n in report.notes)


def test_generation_pilot_a2_budget_gate_and_decision_note(tmp_path):
    # 内存门禁压到 1 字节：A2 门禁不达标 → 触发决策反转评估 note
    from ptde.planner.pilot_run import A2_DECISION_NOTE

    cfg = GenerationPilotConfig(
        entity="pilot_orders", total_rows=500, rows_per_shard=250,
        workers=1, batch_size=200, root_seed=1,
        staging_dir=str(tmp_path / "staging2"), max_memory_bytes=1,
    )
    report = run_generation_pilot(FakeConn, cfg)
    assert not report.within_budget  # 内存门禁不达标
    assert A2_DECISION_NOTE in report.notes


def test_generation_pilot_config_validation():
    with pytest.raises(ValueError, match="必须为正"):
        GenerationPilotConfig(total_rows=0)
    with pytest.raises(ValueError, match="workers"):
        GenerationPilotConfig(workers=0)
    with pytest.raises(ValueError, match="batch_size"):
        GenerationPilotConfig(batch_size=0)


# ---------------------------------------------------------------- A2 全量基准（可选）


def test_generation_pilot_full_million_rows():
    """A2 全量基准：百万级端到端 < 30 分钟（设 PTDE_PILOT_FULL=1 才运行）。"""
    if os.environ.get("PTDE_PILOT_FULL") != "1":
        pytest.skip("A2 全量基准耗时较长：设 PTDE_PILOT_FULL=1 运行（门禁 4）")
    assert A2_PILOT_BUDGET_MINUTES == 30
    report = run_generation_pilot(
        FakeConn,
        GenerationPilotConfig(entity="pilot_orders", total_rows=1_000_000,
                              rows_per_shard=250_000, workers=4,
                              root_seed=42),
    )
    assert report.rows_generated == 1_000_000
    assert report.rows_verified
    assert report.within_budget, f"A2 门禁不达标：{report.duration_ms}ms"
