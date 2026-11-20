"""B7 续：离线组装执行体 + ExecutionManifest 落库层测试。

覆盖：
- manifest 落库层：三列形态读写往返（§12.4 要素 → 平铺列 + JSON 列）；
- OfflinePipeline：sqlite fake DB 通道端到端（注册 → 构建 → READY，
  真组件 build_plan/worker/staging/orchestrator/五层验证器零重造）；
- manifest 落库 + 全链查询（API /manifest 端点；require_complete 全要素）；
- 清理档位执行（DATABASE_RESET 清表 / SCHEMA_REBUILD 重建）；
- 显式失败路径（映射缺失 / Seed 缺失 / 独立验证无上下文）。

执行体为 OfflinePipeline（离线组装）；真库端到端随 A2 真库环境批次接线
（B6 §7F 口径），sqlite 通道为 fake DB 通道不伪装生产可用。
"""

import sqlite3
import threading
import time

import pytest
from sqlalchemy.orm import sessionmaker

from ptde.lifecycle.manifest import missing_elements, require_complete
from ptde.models.dataset import LayerKind
from server.dataset_service import DatasetCancelled, DatasetLifecycleService
from server.db import make_engine
from server.offline_pipeline import (
    EntityMapping,
    OfflinePipeline,
    OfflinePipelineError,
    SqliteCheckpointStore,
)
from server.ptde_repo import PtdeDatasetRepository

# ---------------------------------------------------------------- 夹具

ROWS_SPEC = {
    "name": "订单数据集",
    "targets": {"rows": {"t_order": 100}},
    "reproducibility": {"seed": 42},
}


def _repo() -> PtdeDatasetRepository:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    import server.ptde_repo  # noqa: F401  注册模型
    from server.db import Base

    Base.metadata.create_all(factory.kw["bind"])
    return PtdeDatasetRepository(factory)


def _mapping() -> dict[str, EntityMapping]:
    from ptde.generator.worker import FieldSpec

    return {
        "t_order": EntityMapping(
            entity="t_order",
            fields=(
                FieldSpec(name="id"),
                FieldSpec(name="amount", type_name="DECIMAL(10,2)"),
                FieldSpec(name="status", type_name="VARCHAR(16)"),
            ),
            ddl="CREATE TABLE IF NOT EXISTS t_order "
                "(id INTEGER PRIMARY KEY, amount REAL, status TEXT)",
        ),
    }


def _target_rows(db_path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM t_order").fetchone()[0])
    finally:
        conn.close()


def _make_service(tmp_path, *, mapping=None, spec: dict | None = None,
                  profile_fingerprint: str | None = "prof-fp-1",
                  degradable: bool = True):
    repo = _repo()
    db_path = tmp_path / "target.db"
    pipeline = OfflinePipeline(
        repo, lambda: sqlite3.connect(db_path),
        mapping if mapping is not None else _mapping(),
        staging_base=tmp_path / "staging",
    )
    svc = DatasetLifecycleService(
        repo, pipeline=pipeline,
        degradable_layers=frozenset(
            {LayerKind.DISTRIBUTION, LayerKind.DB_READY}) if degradable else None,
    )
    svc.register_definition("orders", "1", "订单数据集",
                            spec if spec is not None else ROWS_SPEC,
                            profile_fingerprint=profile_fingerprint)
    inst = svc.create_instance("orders", "1", environment_id="dev-sqlite")
    return svc, inst, db_path


def _wait_done(svc, instance_id: str, *, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not svc.task_status(instance_id)["running"]:
            return svc.get_instance(instance_id)
        time.sleep(0.02)
    raise AssertionError("异步任务超时未完成")


# ---------------------------------------------------------------- manifest 落库层


def _manifest_fixture(instance_id: str) -> dict:
    from ptde.models.spec import ExecutionManifest, IdRange, ModelFingerprints, ShardSummary

    return ExecutionManifest(
        id="m-9", instance_id=instance_id, seed=7,
        generator_version="0.1.0", partition_algorithm_version="1",
        model_fingerprints=ModelFingerprints(
            spec="s" * 64, plan="p" * 64, profile="pr" * 32, schema_fp="sc" * 32),
        adapter_capability_snapshot={"chain": ["dbapi_batch_insert"]},
        adapter_version="dbapi_batch_insert@500",
        time_inputs={"start": "2026-01-01"},
        actual_id_ranges={"t_order": IdRange(start=1, end=100)},
        shard_summaries=[ShardSummary(
            shard_id="t_order-0000", entity="t_order", generated_rows=100,
            loaded_rows=100, status="loaded")],
    ).model_dump(mode="json")


def test_repo_manifest_roundtrip():
    repo = _repo()
    row_id = repo.insert_definition(
        def_key="orders", version="1", name="n", spec_json="{}",
        spec_fingerprint="s" * 64)
    instance_id = repo.insert_instance(
        definition_row_id=row_id, definition_version="1",
        environment_label="dev-1")
    from ptde.models.spec import ExecutionManifest

    manifest = ExecutionManifest.model_validate(_manifest_fixture(str(instance_id)))
    row_pk = repo.insert_manifest(instance_id, manifest)
    rows = repo.manifests_for_instance(instance_id)
    assert len(rows) == 1 and rows[0].id == row_pk
    restored = repo.manifest_to_model(rows[0])
    # id 以 DB 行主键字符串形态承载（Instance 同款决策，B7 §4 决策 2）
    assert restored.id == str(row_pk)
    assert restored.instance_id == str(instance_id)
    assert restored.seed == 7
    assert restored.generator_version == "0.1.0"
    assert restored.model_fingerprints.spec == "s" * 64
    assert restored.model_fingerprints.schema_fp == "sc" * 32
    assert restored.adapter_version == "dbapi_batch_insert@500"
    assert restored.time_inputs == {"start": "2026-01-01"}
    assert restored.actual_id_ranges["t_order"].end == 100
    assert restored.shard_summaries[0].status == "loaded"
    # §12.4 全要素零缺项（READY 条件 7 判定输入）
    assert missing_elements(restored) == []


def test_repo_manifest_missing_spec_fingerprint_keeps_row_for_traceability():
    repo = _repo()
    row_id = repo.insert_definition(
        def_key="orders", version="1", name="n", spec_json="{}",
        spec_fingerprint="s" * 64)
    instance_id = repo.insert_instance(
        definition_row_id=row_id, definition_version="1", environment_label="dev-1")
    data = _manifest_fixture(str(instance_id))
    data["model_fingerprints"]["spec"] = None
    from ptde.models.spec import ExecutionManifest

    repo.insert_manifest(instance_id, ExecutionManifest.model_validate(data))
    restored = repo.manifest_to_model(repo.manifests_for_instance(instance_id)[0])
    assert restored.model_fingerprints.spec is None  # 空串哨兵读回 None
    assert "DatasetSpec 指纹" in missing_elements(restored)


# ---------------------------------------------------------------- 离线执行体端到端


def test_offline_pipeline_build_reaches_ready(tmp_path):
    svc, inst, db_path = _make_service(tmp_path)
    accepted = svc.build(inst["id"])
    assert accepted["accepted"] is True
    final = _wait_done(svc, inst["id"])
    assert final["status"] == "READY"
    assert final["validationSummary"]["ready"] is True
    # 目标库实际行数 = 目标行数（count 层全量核对通过的物理证据）
    assert _target_rows(db_path) == 100


def test_offline_pipeline_manifest_complete_and_queryable(tmp_path):
    svc, inst, _ = _make_service(tmp_path)
    svc.build(inst["id"])
    _wait_done(svc, inst["id"])
    summary = svc.instance_manifest(inst["id"])
    assert summary["instanceId"] == inst["id"]
    assert summary["seed"] == 42
    assert summary["generatorVersion"]  # 非哨兵（§12.4 要素）
    assert summary["fingerprints"]["spec"] and summary["fingerprints"]["plan"]
    assert summary["fingerprints"]["profile"] == "prof-fp-1"
    assert summary["fingerprints"]["schema"]
    assert summary["actualIdRanges"]["t_order"]["start"] == 1
    assert summary["actualIdRanges"]["t_order"]["end"] == 100
    assert summary["shardSummaries"], "分片摘要须回填（backfill_from_staging）"
    for shard in summary["shardSummaries"]:
        assert shard["status"] == "loaded"
        assert shard["loaded_rows"] == shard["generated_rows"]
    # 落库读回后仍须全要素完整（require_complete 显式口径）
    repo = svc._repo
    rows = repo.manifests_for_instance(int(inst["id"]))
    restored = repo.manifest_to_model(rows[-1])
    assert require_complete(restored) is restored


def test_offline_pipeline_manifest_id_anchored_on_resume_params(tmp_path):
    svc, inst, db_path = _make_service(tmp_path)
    svc.build(inst["id"], seed=42)
    _wait_done(svc, inst["id"])
    summary = svc.instance_manifest(inst["id"])
    assert summary["fingerprints"]["plan"]  # build_plan 离线规划指纹封存
    assert _target_rows(db_path) == 100


def test_offline_pipeline_cleanup_database_reset_clears_rows(tmp_path):
    svc, inst, db_path = _make_service(tmp_path)
    svc.build(inst["id"])
    _wait_done(svc, inst["id"])
    result = svc.cleanup(
        inst["id"], env_class="dedicated_test_db",
        approvals=[{"operation": "CLEANUP", "target": "t_order",
                    "authorized_by": "op", "ticket": "T-1"}])
    assert result["tier"] == "database_reset"
    assert result["audit"]  # 三级审计快照
    assert _target_rows(db_path) == 0  # DATABASE_RESET：清表（离线通道口径）
    assert svc.get_instance(inst["id"])["status"] == "CLEANED"


def test_offline_pipeline_cleanup_schema_rebuild_recreates_tables(tmp_path):
    svc, inst, db_path = _make_service(tmp_path)
    svc.build(inst["id"])
    _wait_done(svc, inst["id"])
    svc.cleanup(
        inst["id"], env_class="dedicated_test_schema",
        approvals=[{"operation": "CLEANUP", "target": "t_order",
                    "authorized_by": "op", "ticket": "T-1"}])
    assert _target_rows(db_path) == 0  # 重建后为空表且可再装载
    conn = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(t_order)")]
    finally:
        conn.close()
    assert cols == ["id", "amount", "status"]


# ---------------------------------------------------------------- 显式失败路径


def test_offline_pipeline_missing_mapping_entity_fails(tmp_path):
    from ptde.generator.worker import FieldSpec

    mapping = {
        "t_order": EntityMapping(
            entity="t_order", fields=(FieldSpec(name="id"),),
            ddl="CREATE TABLE IF NOT EXISTS t_order (id INTEGER PRIMARY KEY)"),
        "t_extra": EntityMapping(
            entity="t_extra", fields=(FieldSpec(name="id"),),
            ddl="CREATE TABLE IF NOT EXISTS t_extra (id INTEGER PRIMARY KEY)"),
    }
    spec = {"name": "n", "targets": {"rows": {"t_order": 10}},
            "reproducibility": {"seed": 1}}
    svc, inst, _ = _make_service(tmp_path, mapping=mapping, spec=spec)
    svc.build(inst["id"])
    final = _wait_done(svc, inst["id"])
    assert final["status"] == "FAILED"
    assert "映射含分片计划外实体" in final["errorMsg"]


def test_offline_pipeline_missing_seed_fails(tmp_path):
    spec = {"name": "n", "targets": {"rows": {"t_order": 10}}}  # 无 Seed
    svc, inst, _ = _make_service(tmp_path, spec=spec, profile_fingerprint=None)
    svc.build(inst["id"])  # 不传 seed
    final = _wait_done(svc, inst["id"])
    assert final["status"] == "FAILED"
    assert "Seed 缺失" in final["errorMsg"]


def test_offline_pipeline_seed_from_definition_row(tmp_path):
    repo = _repo()
    db_path = tmp_path / "target.db"
    pipeline = OfflinePipeline(
        repo, lambda: sqlite3.connect(db_path), _mapping(),
        staging_base=tmp_path / "staging")
    svc = DatasetLifecycleService(
        repo, pipeline=pipeline,
        degradable_layers=frozenset({LayerKind.DISTRIBUTION, LayerKind.DB_READY}))
    svc.register_definition(
        "orders", "1", "n", ROWS_SPEC, profile_fingerprint="prof-fp-1", seed=99)
    inst = svc.create_instance("orders", "1", environment_id="dev-sqlite")
    svc.build(inst["id"])
    final = _wait_done(svc, inst["id"])
    assert final["status"] == "READY"
    assert svc.instance_manifest(inst["id"])["seed"] == 99  # Definition 行 Seed 回填


def test_offline_pipeline_verify_without_runtime_fails(tmp_path):
    repo = _repo()
    pipeline = OfflinePipeline(
        repo, lambda: sqlite3.connect(":memory:"), _mapping(),
        staging_base=tmp_path / "staging")
    with pytest.raises(OfflinePipelineError, match="缺少离线装配上下文"):
        pipeline.verify({"instance": {"id": "1"}, "params": {"verify_only": True}})


def test_offline_pipeline_rejects_empty_mapping(tmp_path):
    repo = _repo()
    with pytest.raises(OfflinePipelineError, match="entity_mapping 为空"):
        OfflinePipeline(repo, lambda: sqlite3.connect(":memory:"), {},
                        staging_base=tmp_path / "staging")


def test_offline_pipeline_default_gate_refuses_db_ready_warn(tmp_path):
    """默认可降级集（仅 DISTRIBUTION）下，db_ready [未执行] WARN → FAILED（§9.4）。"""
    svc, inst, _ = _make_service(tmp_path, degradable=False)
    svc.build(inst["id"])
    final = _wait_done(svc, inst["id"])
    assert final["status"] == "FAILED"
    failed = [c["name"] for c in final["validationSummary"]["checks"]
              if c["status"] == "FAIL"]
    assert "ready.db_ready" in failed


# ---------------------------------------------------------------- 取消信号触达（B7 续批次 3）


class _CountingEvent(threading.Event):
    """第 n 次 is_set() 起返回 True（模拟分片间收到取消信号）。"""

    def __init__(self, trigger_after: int):
        super().__init__()
        self._calls = 0
        self._trigger_after = trigger_after

    def is_set(self) -> bool:
        self._calls += 1
        if self._calls >= self._trigger_after:
            return True
        return super().is_set()


def test_offline_pipeline_generate_cancel_signal_protocol(tmp_path):
    """取消事件经 ctx 触达 generate 段：入口安全点检查，置位即协作退出。"""
    repo = _repo()
    pipeline = OfflinePipeline(
        repo, lambda: sqlite3.connect(":memory:"), _mapping(),
        staging_base=tmp_path / "staging")
    event = threading.Event()
    event.set()
    with pytest.raises(DatasetCancelled, match="取消信号"):
        pipeline.generate({"instance": {"id": "1"}, "params": {},
                           "cancel_event": event})


def test_offline_pipeline_cancel_between_shards_keeps_generated_shards(tmp_path):
    """分片间安全点协作退出：已产出分片保留（staging 口径：保留供排查）。

    直接消费取消事件验证协议：generate 入口 + 逐分片边界均为安全点
    （服务层 cancel() 经 ctx 注入同一事件，置位即协作退出）。
    """
    repo = _repo()
    spec = {"name": "订单数据集", "targets": {"rows": {"t_order": 60}},
            "reproducibility": {"seed": 1}}
    DatasetLifecycleService(repo).register_definition(
        "orders", "1", "订单数据集", spec, profile_fingerprint="prof-fp-1")
    pipeline = OfflinePipeline(
        repo, lambda: sqlite3.connect(":memory:"), _mapping(),
        staging_base=tmp_path / "staging", rows_per_shard=10)
    # is_set() 第 4 次调用（入口 + 分片 1/2/3 边界）起置位 → 第 3 个分片装完前取消
    event = _CountingEvent(trigger_after=4)
    with pytest.raises(DatasetCancelled, match="取消信号"):
        pipeline.generate({"instance": {"id": "1", "definition_id": "orders@1"},
                           "params": {}, "cancel_event": event})
    staging = pipeline._runtime[1]["staging"]
    # 计划分片 6（60 行 / 每片 10）；staging 记录为逐分片懒登记——已执行 2 片
    # 全部保留（不静默丢弃），余下分片未登记
    assert len(pipeline._runtime[1]["tasks"]) == 6
    totals = staging.totals()
    assert totals["generated"] == 2
    assert totals["pending"] == 0


def test_offline_pipeline_endpoint_probe_wired_into_db_ready(tmp_path):
    """selfCheck 抽检回调接线（B6 §7F）：probe 产出进 db_ready 层 self_check 项。"""
    repo = _repo()
    db_path = tmp_path / "target.db"

    def probe() -> dict:
        return {"checked": 3, "nonEmpty": 2}

    pipeline = OfflinePipeline(
        repo, lambda: sqlite3.connect(db_path), _mapping(),
        staging_base=tmp_path / "staging", endpoint_probe=probe)
    svc = DatasetLifecycleService(
        repo, pipeline=pipeline,
        degradable_layers=frozenset({LayerKind.DISTRIBUTION, LayerKind.DB_READY}))
    svc.register_definition("orders", "1", "订单数据集", ROWS_SPEC,
                            profile_fingerprint="prof-fp-1")
    inst = svc.create_instance("orders", "1", environment_id="dev-sqlite")
    svc.build(inst["id"])
    final = _wait_done(svc, inst["id"])
    assert final["status"] == "READY"
    # 报告层 checks 落库粒度维持既有口径（ptde_validation_report 不落 checks
    # 列）——probe 产出经 pipeline.verify 返回的 ValidationReport 直读断言
    detail = svc.get_instance(inst["id"])
    result = pipeline.verify({"instance": {"id": inst["id"],
                                           "definition_id": detail["definitionId"]},
                              "params": {}})
    db_ready = next(layer for layer in result["report"]["layers"]
                    if layer["layer"] == "db_ready")
    self_check = next(c for c in db_ready["checks"] if c["name"] == "self_check")
    assert self_check["status"] == "PASS"
    assert "nonEmptyRate=0.67" in self_check["detail"]


# ---------------------------------------------------------------- 检查点仓（sqlite）


def test_sqlite_checkpoint_store_roundtrip():
    conn = sqlite3.connect(":memory:")
    store = SqliteCheckpointStore(conn)
    from ptde.loader.checkpoint import ShardCheckpoint, SubmittedState

    cp = ShardCheckpoint(
        instance_id=3, table_name="t_order", shard_id="s-0",
        expected_rows=10, generated_rows=10, loaded_rows=10,
        checksum="ab" * 32, submitted_state=SubmittedState.SUBMITTED.value,
        db_feedback={"rows_loaded": 10})
    store.upsert(cp)
    got = store.get(3, "t_order", "s-0")
    assert got is not None and got.loaded_rows == 10
    assert got.db_feedback == {"rows_loaded": 10}
    chunk = ShardCheckpoint(
        instance_id=3, table_name="t_order", shard_id="s-0", chunk_id=1,
        expected_rows=5, generated_rows=5, loaded_rows=5,
        submitted_state=SubmittedState.SUBMITTED.value)
    store.upsert(chunk)
    listed = store.list_for_instance(3)
    assert [c.chunk_id for c in listed] == [0, 1]  # shard 级汇总行 + chunk 行
    assert store.get(3, "t_order", "s-0", chunk_id=1).last_chunk == 0
