"""Pilot Run 校准协议（R3 T3.4；总方案 §6.5 推荐流程、实施方案 §6.4）。

协议步骤（版本 PILOT_PROTOCOL_VERSION）：
1. 取核心表代表性样本（默认 1 万行，可配）；
2. 经装载通道导入（R3 期间用 DbApiBatchExecutor 降级通道实测；QW4
   LOAD DATA 实测结论作原生通道参照，结论归档见 B5 交接文档）；
3. 采集表大小/索引大小/行数 → 计算 B_i（行均存储成本，实测而非字段长度相加）；
4. 扩样 10× 重测，修正非线性偏差（B_i 取大样本轮次；误差区间取两轮偏差）；
5. 输出 PilotRunReport（可序列化落库）与 CalibratedRowCost。

连接工厂注入式设计（conn_factory: Callable[[], Conn]），不连真库可单测
（FakeConn/FakeCursor 鸭子类型假连接）。

R4.3 扩展（T4.12，实施方案 §7.2；A2 实测门禁）：``run_generation_pilot``
—— 百万级端到端（生成 → staging → 降级装载）实测：
- 复用 R4.3 全链路零件（partitioner → worker → staging/file_format）；
- 装载走 DbApiBatchExecutor 降级通道（executor.py 在 R5 才移 Loader 侧，
  R4 期间 Pilot Run 借用其降级通道——实施方案 §7.3）；
- A2 门禁：端到端 < 30 分钟且峰值内存 < max_memory（门禁 3 分片级控制）；
  不达标 → 触发 A2 决策反转评估（预留编译 worker，协议语言无关）。
"""

from __future__ import annotations

import os
import random
import time
import tracemalloc
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import Field

from ptde.planner.storage_estimator import CalibratedRowCost
from ptde_common.model_base import VersionedModel

__all__ = [
    "PILOT_PROTOCOL_VERSION",
    "A2_PILOT_BUDGET_MINUTES",
    "DEFAULT_PILOT_TOTAL_ROWS",
    "DeterministicRowFactory",
    "GenerationPilotConfig",
    "GenerationPilotReport",
    "PilotRunConfig",
    "PilotRunReport",
    "PilotRound",
    "SqlStorageProbe",
    "StorageProbe",
    "run_generation_pilot",
    "run_pilot",
]

PILOT_PROTOCOL_VERSION = "1"
DEFAULT_SAMPLE_ROWS = 10_000
DEFAULT_SCALE_MULTIPLIER = 10
DEFAULT_BATCH_SIZE = 500  # 对齐 legacy DbApiBatchExecutor DEFAULT_BATCH_SIZE 语义
SINGLE_ROUND_ERROR_BAND = "±20%（仅单轮样本，未做扩样非线性修正）"

# R4.3 T4.12（A2 实测门禁）
A2_PILOT_BUDGET_MINUTES = 30  # 百万级端到端 < 30 分钟（A2 基准）
DEFAULT_PILOT_TOTAL_ROWS = 1_000_000
DEFAULT_PILOT_SHARD_ROWS = 250_000
DEFAULT_PILOT_WORKERS = 4
DEFAULT_PILOT_MEMORY_BUDGET = 8 * 1024**3  # 门禁 3：8GB 预算内的分片级控制
A2_DECISION_NOTE = (
    "A2 门禁不达标 → 触发 A2 决策反转评估（预留编译 worker，分片协议语言无关）"
)


class StorageProbe(Protocol):
    """存储度量探针：返回 (table_bytes, index_bytes, row_count)——鸭子类型可替换。"""

    def measure(self, conn: Any, table: str, include_indexes: bool = True
                ) -> tuple[int, int, int]: ...


class SqlStorageProbe:
    """SQL 存储度量探针（MySQL information_schema / PG 系统函数）。"""

    MYSQL_SQL = (
        "SELECT COALESCE(data_length, 0), COALESCE(index_length, 0), "
        "COALESCE(table_rows, 0) FROM information_schema.TABLES "
        "WHERE table_schema = DATABASE() AND table_name = %s"
    )
    PG_SQL = (
        "SELECT pg_relation_size(%s), pg_indexes_size(%s), "
        "(SELECT reltuples::bigint FROM pg_class WHERE relname = %s)"
    )

    def __init__(self, dialect: str) -> None:
        self.dialect = (dialect or "mysql").lower()

    def measure(self, conn: Any, table: str, include_indexes: bool = True
                ) -> tuple[int, int, int]:
        cur = conn.cursor()
        try:
            if self.dialect in ("postgres", "postgresql"):
                cur.execute(self.PG_SQL, (table, table, table))
            else:
                cur.execute(self.MYSQL_SQL, (table,))
            row = cur.fetchone()
        finally:
            cur.close()
        if row is None:
            return 0, 0, 0
        table_bytes = int(row[0] or 0)
        index_bytes = int(row[1] or 0) if include_indexes else 0
        row_count = int(row[2] or 0)
        return table_bytes, index_bytes, row_count


class DeterministicRowFactory:
    """代表性样本行生成（确定性：注入 random.Random；值域覆盖可变长度与 NULL）。"""

    def __init__(self, columns: list[tuple[str, str]], rng: random.Random) -> None:
        self.columns = columns
        self.rng = rng

    def make_row(self, seq: int) -> list[Any]:
        row: list[Any] = []
        for _name, type_name in self.columns:
            t = type_name.lower()
            if "bigint" in t or "int" in t:
                row.append(self.rng.randint(1, 1_000_000) if seq <= 0 else seq)
            elif "decimal" in t or "numeric" in t:
                row.append(round(self.rng.uniform(0, 10_000), 2))
            elif "datetime" in t or "timestamp" in t:
                row.append(f"2026-{1 + seq % 12:02d}-{1 + seq % 28:02d} "
                           f"{seq % 24:02d}:00:00")
            elif "char" in t or "text" in t:
                row.append("v" * self.rng.randint(1, 32))
            else:
                row.append(self.rng.randint(0, 255))
        return row


class PilotRunConfig(VersionedModel):
    """Pilot Run 配置（默认 1 万行 → 扩样 10×）。"""

    sample_rows: int = DEFAULT_SAMPLE_ROWS
    scale_multiplier: int = DEFAULT_SCALE_MULTIPLIER
    batch_size: int = DEFAULT_BATCH_SIZE
    include_indexes: bool = True

    def model_post_init(self, __context: Any) -> None:
        if self.sample_rows <= 0:
            raise ValueError("sample_rows 必须为正整数")
        if self.scale_multiplier <= 1:
            raise ValueError("scale_multiplier 必须 > 1（扩样重测修正非线性偏差）")
        if self.batch_size <= 0:
            raise ValueError("batch_size 必须为正整数")


class PilotRound(VersionedModel):
    """单轮试装载度量。"""

    rows_loaded: int
    table_bytes: int
    index_bytes: int
    delta_bytes: int  # 相对上一轮的字节增量
    bytes_per_row: float  # 实测行均存储成本（表 + 索引）


class PilotRunReport(VersionedModel):
    """Pilot Run 报告（可序列化落库；B_i 实测值 + 误差区间）。"""

    table: str
    channel: str = "dbapi_batch"  # R3 降级通道；原生通道参照 QW4 实测结论
    protocol_version: str = PILOT_PROTOCOL_VERSION
    rounds: list[PilotRound] = Field(default_factory=list)
    bytes_per_row: float = 0.0
    index_bytes_per_row: float = 0.0
    error_band: str | None = None
    rows_verified: bool = False  # 装载行数经探针核对（数量复核锚点）
    collected_at: datetime | None = None

    exclude_fingerprint_fields = frozenset({"collected_at"})

    def to_calibration(self) -> CalibratedRowCost:
        """报告 → 校准记录（storage_estimator 消费）。"""
        return CalibratedRowCost(
            table=self.table,
            bytes_per_row=self.bytes_per_row,
            index_bytes_per_row=self.index_bytes_per_row,
            sample_rows=self.rounds[-1].rows_loaded if self.rounds else 0,
            error_band=self.error_band,
            calibrated=self.bytes_per_row > 0,
        )


def run_pilot(
    table: str,
    columns: list[tuple[str, str]],
    conn_factory: Callable[[], Any],
    config: PilotRunConfig | None = None,
    probe: StorageProbe | None = None,
    rng: random.Random | None = None,
    insert_sql: str | None = None,
) -> PilotRunReport:
    """执行 Pilot Run 校准（连接工厂注入；装载走 DbApiBatchExecutor 降级通道）。

    insert_sql 缺省按列清单构造；列清单为 (name, db_type) 二元组。
    """
    from ptde.adapters.direct.executor import (
        DbApiBatchExecutor,  # R3 降级通道（legacy 借用，不改本体）
    )

    cfg = config or PilotRunConfig()
    rng = rng or random.Random(0)
    probe = probe or SqlStorageProbe("mysql")
    col_names = [c[0] for c in columns]
    sql = insert_sql or (
        f"INSERT INTO {table} ({', '.join(col_names)}) VALUES "
        f"({', '.join(['%s'] * len(col_names))})"
    )

    conn = conn_factory()
    executor = DbApiBatchExecutor(cfg.batch_size)
    factory = DeterministicRowFactory(columns, rng)

    report = PilotRunReport(table=table, collected_at=datetime.now(UTC))
    # 基线度量（装载前）：本轮协议的字节增量均相对基线，排除表既有数据/页开销
    base_t, base_i, base_rows = probe.measure(conn, table, cfg.include_indexes)
    prev_bytes = base_t + base_i
    prev_rows = base_rows
    prev_loaded = 0
    rows_planned = cfg.sample_rows
    loaded_so_far = 0
    while True:
        batch_rows = [factory.make_row(prev_loaded + i) for i in range(rows_planned)]
        for start in range(0, len(batch_rows), cfg.batch_size):
            executor.execute_batch(conn, sql, batch_rows[start:start + cfg.batch_size])
        loaded_so_far += len(batch_rows)

        t_bytes, i_bytes, db_rows = probe.measure(conn, table, cfg.include_indexes)
        delta = (t_bytes + i_bytes) - prev_bytes
        per_row = delta / (loaded_so_far - prev_loaded) if loaded_so_far > prev_loaded else 0.0
        report.rounds.append(
            PilotRound(
                rows_loaded=loaded_so_far,
                table_bytes=t_bytes,
                index_bytes=i_bytes,
                delta_bytes=delta,
                bytes_per_row=round(per_row, 4),
            )
        )
        # 数量复核锚点：探针行数增量与装载行数一致（不静默吞差异）
        if db_rows - prev_rows == loaded_so_far - prev_loaded:
            report.rows_verified = True
        prev_bytes = t_bytes + i_bytes
        prev_rows = db_rows
        prev_loaded = loaded_so_far
        if rows_planned >= cfg.sample_rows * cfg.scale_multiplier:
            break
        rows_planned = cfg.sample_rows * cfg.scale_multiplier

    # 非线性修正：B_i 取大样本轮次（表/索引成本分开归因）；误差区间取两轮偏差
    if len(report.rounds) >= 2:
        first, last = report.rounds[0], report.rounds[-1]
        last_rows = last.rows_loaded - first.rows_loaded
        if last_rows > 0:
            table_per_row = (last.table_bytes - first.table_bytes) / last_rows
            report.index_bytes_per_row = max(0.0, last.bytes_per_row - table_per_row)
            report.bytes_per_row = table_per_row
        else:
            report.bytes_per_row = last.bytes_per_row
        if last.bytes_per_row > 0:
            rel = abs(first.bytes_per_row - last.bytes_per_row) / last.bytes_per_row
            report.error_band = f"±{rel * 100:.1f}%（两轮扩样校准：{first.rows_loaded}"\
                                f" → {last.rows_loaded} 行）"
    elif report.rounds:
        report.bytes_per_row = report.rounds[0].bytes_per_row
        report.error_band = SINGLE_ROUND_ERROR_BAND
    report.seal()
    return report


# ============================================================ R4.3 T4.12 扩展


class GenerationPilotConfig(VersionedModel):
    """A2 生成 Pilot 配置（分片/Worker/预算均可配；默认百万级 4 Worker）。"""

    entity: str = "pilot_orders"
    total_rows: int = DEFAULT_PILOT_TOTAL_ROWS
    rows_per_shard: int = DEFAULT_PILOT_SHARD_ROWS
    workers: int = DEFAULT_PILOT_WORKERS
    batch_size: int = DEFAULT_BATCH_SIZE
    max_memory_bytes: int = DEFAULT_PILOT_MEMORY_BUDGET  # 门禁 3：分片级内存控制
    budget_minutes: int = A2_PILOT_BUDGET_MINUTES  # A2：端到端 < 30 分钟
    root_seed: int | None = None  # None = 显式不可复现路径（A3）
    staging_dir: str | None = None  # 缺省 tmp 下按 entity 建目录

    def model_post_init(self, __context: Any) -> None:
        if self.total_rows <= 0 or self.rows_per_shard <= 0:
            raise ValueError("total_rows 与 rows_per_shard 必须为正（显式失败）")
        if self.workers < 1:
            raise ValueError("workers 必须 ≥ 1（显式失败）")
        if self.batch_size <= 0:
            raise ValueError("batch_size 必须为正（显式失败）")


class GenerationPilotReport(VersionedModel):
    """A2 生成 Pilot 报告（可序列化落库；门禁 3/4 实测口径）。"""

    entity: str
    total_rows: int = 0
    rows_generated: int = 0
    rows_loaded: int | None = None  # conn_factory 缺省时跳过装载
    shards: int = 0
    workers: int = 1
    generation_ms: int = 0
    load_ms: int = 0
    duration_ms: int = 0
    peak_memory_bytes: int = 0  # 主进程 tracemalloc 峰值（门禁 3 观测口径）
    channel: str = "dbapi_batch"  # 降级通道；原生通道随 R5 Adapter 落地
    rows_verified: bool = False  # rows_loaded == rows_generated 数量复核
    within_budget: bool = False  # 时长 < budget_minutes 且内存 < max_memory
    collected_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)

    exclude_fingerprint_fields = frozenset({"collected_at"})


def default_pilot_fields() -> list:
    """A2 Pilot 默认字段：覆盖主键区间 / 分布采样 / 列类型默认三类生成路径。"""
    from ptde.generator.worker import FieldSpec
    from ptde.models.profile import DistributionFit

    return [
        FieldSpec(name="id", type_name="BIGINT"),
        FieldSpec(name="amount", type_name="DECIMAL",
                  distribution=DistributionFit(
                      type="lognormal", params={"mu": 3.0, "sigma": 0.5})),
        FieldSpec(name="status", type_name="VARCHAR",
                  distribution=DistributionFit(
                      type="weighted_categorical",
                      params={"values": ["PENDING", "PAID", "CANCELLED"],
                              "weights": [0.3, 0.6, 0.1]})),
        FieldSpec(name="note", type_name="VARCHAR"),  # 无分布 → 列类型默认生成
    ]


def run_generation_pilot(
    conn_factory: Callable[[], Any] | None,
    config: GenerationPilotConfig | None = None,
    fields: Sequence | None = None,
    table: str | None = None,
) -> GenerationPilotReport:
    """A2 生成 Pilot：分片规划 → 多进程生成 → staging → 降级装载（实测）。

    - 复用 R4.3 零件（partitioner/worker/staging/file_format），本函数只做
      端到端编排与度量，不另起生成路径；
    - conn_factory 为 None → 只跑生成+staging（不连库单测/干跑），rows_loaded
      显式 None 并在 notes 说明（不伪装已装载——A3）；
    - 装载走 DbApiBatchExecutor 降级通道（R4 借用 legacy，R5 归 Loader）；
      CSV 直传为字符串形态，类型转换由 DB 端完成（与 LOAD DATA 语义一致）。
    """
    from ptde.generator.keys import IdRangeAllocator
    from ptde.generator.partitioner import plan_shards
    from ptde.generator.staging import StagingArea
    from ptde.generator.worker import run_shards, shard_tasks_from_plan

    cfg = config or GenerationPilotConfig()
    fields = list(fields or default_pilot_fields())
    table = table or cfg.entity
    report = GenerationPilotReport(
        entity=cfg.entity, total_rows=cfg.total_rows, workers=cfg.workers,
        collected_at=datetime.now(UTC),
    )

    # 1. 分片规划（区间经 §7.6 三重检查）+ staging 登记
    staging_dir = cfg.staging_dir or f"/tmp/ptde_pilot_{cfg.entity}_{os.getpid()}"
    staging = StagingArea(staging_dir, plan_fingerprint=f"pilot:{cfg.entity}")
    plan = plan_shards({cfg.entity: cfg.total_rows}, rows_per_shard=cfg.rows_per_shard,
                       id_allocator=IdRangeAllocator())
    report.shards = len(plan.shards)
    tasks = shard_tasks_from_plan(plan, {cfg.entity: fields},
                                  root_seed=cfg.root_seed,
                                  plan_fingerprint=f"pilot:{cfg.entity}",
                                  staging=staging)

    # 2. 多进程生成（Worker 只产数据；峰值内存 tracemalloc 观测）
    tracemalloc.start()
    t0 = time.monotonic()
    results = run_shards(tasks, workers=cfg.workers, staging=staging)
    report.generation_ms = int((time.monotonic() - t0) * 1000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    report.peak_memory_bytes = peak
    report.rows_generated = sum(r.generated_rows for r in results)

    # 3. 降级通道装载（conn_factory 注入；None → 干跑跳过并显式说明）
    if conn_factory is None:
        report.notes.append(
            "conn_factory 未提供：仅执行生成+staging（干跑），未装载——不伪装已装载"
        )
        report.duration_ms = report.generation_ms
        report.within_budget = (
            report.duration_ms < cfg.budget_minutes * 60_000
            and report.peak_memory_bytes <= cfg.max_memory_bytes
        )
        report.seal()
        return report

    from ptde.adapters.direct.executor import DbApiBatchExecutor  # R4 借用 legacy 降级通道
    from ptde.generator.file_format import read_shard_file

    col_names = [f.name for f in fields]
    sql = (f"INSERT INTO {table} ({', '.join(col_names)}) VALUES "
           f"({', '.join(['%s'] * len(col_names))})")
    executor = DbApiBatchExecutor(cfg.batch_size)
    conn = conn_factory()
    t1 = time.monotonic()
    loaded = 0
    for result in results:
        # 逐分片批量装载（staging → Loader；错误记录不静默丢弃由 R5 承接）
        rows = [row for _h, row in read_shard_file(result.path)]
        for start in range(0, len(rows), cfg.batch_size):
            executor.execute_batch(conn, sql, rows[start:start + cfg.batch_size])
        loaded += len(rows)
        staging.mark_loaded(result.shard_id, len(rows))  # 数量复核锚点
    report.load_ms = int((time.monotonic() - t1) * 1000)
    report.rows_loaded = loaded
    report.rows_verified = loaded == report.rows_generated
    report.duration_ms = report.generation_ms + report.load_ms

    totals = staging.verify_complete()  # 检查点协议终态校验（不静默放行）
    if totals["rows_loaded"] != report.rows_generated:
        raise ValueError("staging 汇总行数与装载行数不一致（显式失败）")

    # 4. A2 门禁判定：时长 + 内存双达标
    report.within_budget = (
        report.duration_ms < cfg.budget_minutes * 60_000
        and report.peak_memory_bytes <= cfg.max_memory_bytes
    )
    if not report.within_budget:
        report.notes.append(A2_DECISION_NOTE)
    report.seal()
    return report
