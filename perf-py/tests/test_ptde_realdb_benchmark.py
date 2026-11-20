"""B7 续批次 7：真库端到端冒烟 + R5 真库装载基准（PTDE_REALDB_URL 门控）。

环境门控：``PTDE_REALDB_URL``（mysql://user:pwd@host:port/db）未设置 → 整组
skip（与 test_generation_pilot_full_million_rows 的 PTDE_PILOT_FULL 门控同口径）：
- 冒烟（默认 2000 行）：全链 build → READY，原生 LOAD DATA LOCAL INFILE 档
  生效（db_feedback strategy 断言），count 层全量核对通过；
- 装载基准（``PTDE_REALDB_BENCH_ROWS``，默认百万级）：generate/load 分段计时，
  报告归档 ``benchmarks/realdb_load_benchmark.json``（R5 真库装载基准，
  B5 §5 ⏳ 项闭合）。

前置：目标库可写；``local_infile=ON``（服务端）+ 连接 local_infile=True
（conn 工厂内置）；ptde_* 元数据表由 sql/init.sql 就绪（或 PERF_PY_DATABASE_URL
指向的库内 create_all）。
"""

import json
import os
import time
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from ptde.models.dataset import LayerKind
from server.dataset_service import DatasetLifecycleService
from server.db import make_engine
from server.ptde_repo import PtdeDatasetRepository
from server.realdb_pipeline import REALDB_URL_ENV, build_realdb_pipeline, realdb_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(REALDB_URL_ENV),
    reason=f"真库批次：需 {REALDB_URL_ENV}（mysql://user:pwd@host:port/db）",
)

ENTITY = "ptde_bench_orders"
FIELDS = {
    ENTITY: [
        {"name": "id", "type_name": "BIGINT"},
        {"name": "amount", "type_name": "DECIMAL"},
        {"name": "status", "type_name": "VARCHAR"},
    ]
}


def _repo() -> PtdeDatasetRepository:
    factory = sessionmaker(bind=make_engine(os.environ.get(
        "PERF_PY_DATABASE_URL", "sqlite:///:memory:")), expire_on_commit=False)
    import server.ptde_repo  # noqa: F401  注册模型
    from server.db import Base

    Base.metadata.create_all(factory.kw["bind"])
    return PtdeDatasetRepository(factory)


def _fresh_pipeline(tmp_path, repo: PtdeDatasetRepository, workers: int = 1):
    """清空目标表/检查点表后装配执行体（幂等重跑口径；repo 与 service 共用）。"""
    from server.realdb_pipeline import mysql_conn_factory

    conn = mysql_conn_factory(realdb_url())()
    cur = conn.cursor()
    try:
        cur.execute(f"DROP TABLE IF EXISTS {ENTITY}")
        cur.execute("DROP TABLE IF EXISTS ptde_shard_checkpoint")
    finally:
        cur.close()
    conn.commit()
    return build_realdb_pipeline(
        repo, realdb_url(), FIELDS,
        staging_base=tmp_path / "staging", workers=workers)


def _scalar(conn, sql: str):
    cur = conn.cursor()
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


# ---------------------------------------------------------------- 真库端到端冒烟


def test_realdb_channel_native_loaddata_reaches_ready(tmp_path):
    from server.realdb_pipeline import mysql_conn_factory

    rows = int(os.environ.get("PTDE_REALDB_SMOKE_ROWS", "2000"))
    conn = mysql_conn_factory(realdb_url())()
    repo = _repo()
    svc = DatasetLifecycleService(
        repo,
        pipeline=_fresh_pipeline(tmp_path, repo),
        degradable_layers=frozenset({LayerKind.DISTRIBUTION, LayerKind.DB_READY}))
    svc.register_definition(
        "bench", "1", "真库冒烟",
        {"name": "真库冒烟", "targets": {"rows": {ENTITY: rows}},
         "reproducibility": {"seed": 42}},
        profile_fingerprint="prof-fp-1")
    inst = svc.create_instance("bench", "1", environment_id="realdb-mysql")
    assert svc.build(inst["id"])["accepted"] is True
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if not svc.task_status(inst["id"])["running"]:
            break
        time.sleep(0.1)
    final = svc.get_instance(inst["id"])
    assert final["status"] == "READY", final.get("errorMsg")
    # count 层全量核对通过的物理证据：真库实际行数 = 目标行数
    assert int(_scalar(conn, f"SELECT COUNT(*) FROM {ENTITY}")) == rows
    # 原生 LOAD DATA LOCAL INFILE 档生效（非降级批量 INSERT）
    cur = conn.cursor()
    try:
        cur.execute("SELECT db_feedback_json FROM ptde_shard_checkpoint "
                    "WHERE chunk_id = 0")
        feedbacks = [r[0] for r in cur.fetchall()]
    finally:
        cur.close()
    assert feedbacks, "shard 级检查点须落库（DbCheckpointStore）"
    for fb in feedbacks:
        assert json.loads(fb)["strategy"] == "load_data_local"


# ---------------------------------------------------------------- R5 真库装载基准


def test_realdb_load_benchmark_native_channel(tmp_path):
    """R5 真库装载基准：原生链 generate/load 分段实测，报告归档 benchmarks/。

    行数经 PTDE_REALDB_BENCH_ROWS 配置（默认百万级）；结果与 B5 §5 ⏳ 项
    及 A2 降级通道基准（百万级 12.79s）对照归档。
    """
    from server.realdb_pipeline import mysql_conn_factory

    rows = int(os.environ.get("PTDE_REALDB_BENCH_ROWS", "1_000_000"))
    workers = int(os.environ.get("PTDE_REALDB_BENCH_WORKERS", "4"))
    repo = _repo()
    pipeline = _fresh_pipeline(tmp_path, repo, workers=workers)
    svc = DatasetLifecycleService(repo)  # 仅注册 Definition（不占用执行编排）
    svc.register_definition(
        "bench", "1", "真库装载基准",
        {"name": "真库装载基准", "targets": {"rows": {ENTITY: rows}},
         "reproducibility": {"seed": 42}},
        profile_fingerprint="prof-fp-1")
    inst = svc.create_instance("bench", "1", environment_id="realdb-mysql")
    inst_id = inst["id"]

    conn = mysql_conn_factory(realdb_url())()
    t0 = time.monotonic()
    gen = pipeline.generate({"instance": {"id": inst_id,
                                          "definition_id": "bench@1"},
                             "params": {}})
    generate_ms = int((time.monotonic() - t0) * 1000)
    t1 = time.monotonic()
    load = pipeline.load({"instance": {"id": inst_id}})
    load_ms = int((time.monotonic() - t1) * 1000)

    assert gen["generated_rows"] == rows
    assert load["loaded_rows"] == rows
    db_rows = int(_scalar(conn, f"SELECT COUNT(*) FROM {ENTITY}"))
    assert db_rows == rows  # 数量复核锚点
    # 原生档生效（非降级批量 INSERT——若降级，基准须以降级口径归档并显式说明）
    cur = conn.cursor()
    try:
        cur.execute("SELECT db_feedback_json FROM ptde_shard_checkpoint "
                    "WHERE chunk_id = 0")
        strategies = {json.loads(r[0])["strategy"] for r in cur.fetchall()}
    finally:
        cur.close()
    assert strategies == {"load_data_local"}

    report = {
        "benchmark": "realdb_load_native_channel",
        "product": "mysql",
        "entity": ENTITY,
        "rows": rows,
        "shards": gen["shards"],
        "workers": workers,
        "generate_ms": generate_ms,
        "load_ms": load_ms,
        "load_rows_per_sec": round(rows / (load_ms / 1000), 1) if load_ms else None,
        "total_ms": generate_ms + load_ms,
        "strategies": sorted(strategies),
        "a2_degraded_channel_reference_ms": 12790,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    out = Path(__file__).resolve().parents[1] / "benchmarks"
    out.mkdir(exist_ok=True)
    (out / "realdb_load_benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
