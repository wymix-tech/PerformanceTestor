"""T5.7 分片检查点与断点恢复测试（实施方案 §8.2/§12.4；总方案 §9.6）。

覆盖：§12.4 DDL 契约、upsert 幂等 SQL 形态、staging manifest ↔ DB 检查点
双向核对（§8.6 门禁 1/2：不重复不遗漏）、BulkLoadResult 快照消费、§9.6
恢复流程分类（已完成/未开始/部分提交）、多 chunk 非全文件原子核对、
不一致显式失败（A3）。
"""

from __future__ import annotations

import pytest

from ptde.adapters.base import BulkLoadResult
from ptde.generator.file_format import ShardFileMeta
from ptde.generator.staging import ShardStatus, StagingArea
from ptde.loader.checkpoint import (
    DDL_PTDE_SHARD_CHECKPOINT,
    CheckpointCoordinator,
    CheckpointStoreError,
    DbCheckpointStore,
    RecoveryAction,
    ShardCheckpoint,
    SubmittedState,
)

INSTANCE_ID = 42


# ============================================================ 测试桩


class FakeStore:
    """检查点仓测试桩（接口对齐 DbCheckpointStore；字典后端确定性）。"""

    def __init__(self) -> None:
        self._rows: dict[tuple, ShardCheckpoint] = {}

    def upsert(self, cp: ShardCheckpoint) -> None:
        key = (cp.instance_id, cp.table_name, cp.shard_id, cp.chunk_id)
        self._rows[key] = cp.model_copy(deep=True)

    def get(self, instance_id: int, table_name: str, shard_id: str,
            chunk_id: int = 0) -> ShardCheckpoint | None:
        row = self._rows.get((instance_id, table_name, shard_id, chunk_id))
        return row.model_copy(deep=True) if row else None

    def list_for_instance(self, instance_id: int,
                          table_name: str | None = None) -> list[ShardCheckpoint]:
        return [
            cp.model_copy(deep=True)
            for key, cp in sorted(self._rows.items())
            if key[0] == instance_id and (table_name is None or key[1] == table_name)
        ]


def _staging(tmp_path) -> StagingArea:
    staging = StagingArea(tmp_path, plan_fingerprint="fp:test")
    for entity, shards in (("orders", [("s0", 100), ("s1", 200)]),
                           ("customers", [("c0", 50)])):
        for shard_id, rows in shards:
            staging.register(entity, shard_id)
            staging.mark_generated(shard_id, ShardFileMeta(
                path=str(tmp_path / entity / f"{shard_id}.csv"),
                rows=rows, checksum="a" * 64,
            ))
    staging.checkpoint()
    return staging


def _coordinator(tmp_path) -> tuple[CheckpointCoordinator, StagingArea, FakeStore]:
    staging = _staging(tmp_path)
    store = FakeStore()
    coord = CheckpointCoordinator(staging, store, INSTANCE_ID)
    for record in staging.records():  # 现实流程：全部分片生成后登记检查点
        coord.record_generation(record.shard_id)
    return coord, staging, store


def _result(shard_id: str, entity: str, rows: int) -> BulkLoadResult:
    return BulkLoadResult(
        shard_id=shard_id, entity=entity, table=entity,
        strategy="load_data_local", rows_expected=rows,
        rows_loaded=rows, rows_verified=True,
    )


# ============================================================ §12.4 DDL 与存储仓


def test_ddl_matches_spec_12_4() -> None:
    assert "ptde_shard_checkpoint" in DDL_PTDE_SHARD_CHECKPOINT
    assert "uk_shard_chunk (instance_id, table_name, shard_id, chunk_id)" in DDL_PTDE_SHARD_CHECKPOINT
    assert "PENDING/PARTIAL/SUBMITTED/CONFIRMED/CLEANED" in DDL_PTDE_SHARD_CHECKPOINT
    assert "CREATE TABLE IF NOT EXISTS" in DDL_PTDE_SHARD_CHECKPOINT


class StoreFakeCursor:
    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.executed_sql.append(sql)
        if sql.startswith("SELECT"):
            key = tuple(params)
            self._conn._result = [self._conn.rows[key]] if key in self._conn.rows else []

    def fetchall(self):
        return self._conn._result

    def fetchone(self):
        return self._conn._result[0] if self._conn._result else None

    def close(self):
        pass


class StoreFakeConn:
    """脚本化假连接：DDL/INSERT 落 SQL 日志，SELECT 按 key 取行。"""

    def __init__(self) -> None:
        self.executed_sql: list[str] = []
        self.rows: dict[tuple, tuple] = {}
        self._result: list[tuple] = []
        self.committed = 0

    def cursor(self) -> StoreFakeCursor:
        return StoreFakeCursor(self)

    def commit(self) -> None:
        self.committed += 1


def _row_tuple(cp: ShardCheckpoint) -> tuple:
    import json

    return (
        cp.instance_id, cp.table_name, cp.shard_id, cp.chunk_id,
        cp.expected_rows, cp.generated_rows, cp.loaded_rows, cp.checksum,
        cp.last_chunk, cp.submitted_state,
        json.dumps(cp.db_feedback, ensure_ascii=False) if cp.db_feedback else None,
        cp.error_msg,
    )


def test_store_ensures_table_and_upsert_sql_shape() -> None:
    conn = StoreFakeConn()
    store = DbCheckpointStore(conn)
    assert conn.executed_sql[0] == DDL_PTDE_SHARD_CHECKPOINT
    assert conn.committed == 1
    cp = ShardCheckpoint(instance_id=1, table_name="orders", shard_id="s0",
                         expected_rows=10, generated_rows=10)
    store.upsert(cp)
    insert = next(s for s in conn.executed_sql if s.startswith("INSERT"))
    assert "ON DUPLICATE KEY UPDATE" in insert
    assert "submitted_state = VALUES(submitted_state)" in insert


def test_store_upsert_get_roundtrip_with_feedback_snapshot() -> None:
    conn = StoreFakeConn()
    store = DbCheckpointStore(conn)
    result = _result("s0", "orders", 100)
    cp = ShardCheckpoint(
        instance_id=INSTANCE_ID, table_name="orders", shard_id="s0",
        expected_rows=100, generated_rows=100, loaded_rows=100,
        checksum="a" * 64, submitted_state=SubmittedState.SUBMITTED.value,
        db_feedback=result.to_snapshot(),
    )
    store.upsert(cp)
    conn.rows[(INSTANCE_ID, "orders", "s0", 0)] = _row_tuple(cp)
    got = store.get(INSTANCE_ID, "orders", "s0")
    assert got is not None and got.submitted_state == "SUBMITTED"
    assert got.db_feedback["strategy"] == "load_data_local"
    assert got.db_feedback["rows_verified"] is True


# ============================================================ Coordinator：记录侧


def test_record_generation_persists_pending_checkpoint(tmp_path) -> None:
    coord, staging, store = _coordinator(tmp_path)
    cp = coord.record_generation("s0")
    assert cp.submitted_state == SubmittedState.PENDING.value
    assert cp.expected_rows == 100 and cp.generated_rows == 100
    assert cp.checksum == "a" * 64
    stored = store.get(INSTANCE_ID, "orders", "s0")
    assert stored is not None and stored.shard_id == "s0"


def test_record_generation_rejects_non_generated(tmp_path) -> None:
    staging = StagingArea(tmp_path, plan_fingerprint="fp")
    staging.register("orders", "s5")
    store = FakeStore()
    coord = CheckpointCoordinator(staging, store, INSTANCE_ID)
    with pytest.raises(CheckpointStoreError, match="generated"):
        coord.record_generation("s5")


def test_record_bulkload_result_syncs_staging_and_store(tmp_path) -> None:
    coord, staging, store = _coordinator(tmp_path)
    cp = coord.record_bulkload_result(_result("s1", "orders", 200))
    # staging 状态机推进（行数复核锚点复用，不另起一套）
    assert staging.totals()["loaded"] == 1
    # DB 侧 SUBMITTED + BulkLoadResult 快照原样落库（不重复不遗漏）
    assert cp.submitted_state == SubmittedState.SUBMITTED.value
    assert cp.loaded_rows == 200
    assert cp.db_feedback["strategy"] == "load_data_local"
    assert cp.db_feedback["rows_expected"] == 200


def test_record_bulkload_result_row_mismatch_fails(tmp_path) -> None:
    coord, _staging, _store = _coordinator(tmp_path)
    bad = BulkLoadResult(
        shard_id="s0", entity="orders", table="orders",
        strategy="load_data_local", rows_expected=100, rows_loaded=99,
    )
    with pytest.raises(CheckpointStoreError, match="99"):
        coord.record_bulkload_result(bad)


def test_record_unknown_shard_fails(tmp_path) -> None:
    coord, _staging, _store = _coordinator(tmp_path)
    with pytest.raises(CheckpointStoreError, match="未在 staging manifest 登记"):
        coord.record_bulkload_result(_result("ghost", "orders", 10))


def test_record_chunk_progress(tmp_path) -> None:
    coord, _staging, store = _coordinator(tmp_path)
    coord.record_chunk("s0", 1, 60, 60)
    coord.record_chunk("s0", 2, 40, 40)
    chunks = [c for c in store.list_for_instance(INSTANCE_ID, "orders")
              if c.shard_id == "s0" and c.chunk_id > 0]
    assert [(c.chunk_id, c.loaded_rows) for c in chunks] == [(1, 60), (2, 40)]


# ============================================================ §9.6 恢复流程


def _loaded(staging: StagingArea, shard_id: str, rows: int) -> None:
    staging.mark_loaded(shard_id, rows)


def test_plan_recovery_classifies_pending_generated(tmp_path) -> None:
    coord, staging, store = _coordinator(tmp_path)
    coord.record_generation("s0")
    plan = coord.plan_recovery(lambda record: 0)
    actions = {a["shard_id"]: a["action"] for a in plan.actions}
    assert actions["s0"] == RecoveryAction.LOAD.value  # 已生成未装载
    assert actions["s1"] == RecoveryAction.LOAD.value
    assert actions["c0"] == RecoveryAction.LOAD.value
    # pending 分片需先生成（本轮全部已 generated，故无 GENERATE）
    assert RecoveryAction.GENERATE.value not in actions.values()
    plan.require_recoverable()  # 无不一致


def test_plan_recovery_pending_shard_records_generate(tmp_path) -> None:
    staging = StagingArea(tmp_path, plan_fingerprint="fp")
    staging.register("orders", "s_new")  # 保持 pending
    store = FakeStore()
    coord = CheckpointCoordinator(staging, store, INSTANCE_ID)
    plan = coord.plan_recovery(lambda record: 0)
    assert plan.shards_with_action(RecoveryAction.GENERATE) == ["s_new"]


def test_plan_recovery_confirmed_when_actual_matches(tmp_path) -> None:
    coord, staging, store = _coordinator(tmp_path)
    coord.record_bulkload_result(_result("s0", "orders", 100))
    plan = coord.plan_recovery(lambda record: 100)
    actions = {a["shard_id"]: a["action"] for a in plan.actions}
    assert actions["s0"] == RecoveryAction.CONFIRM.value
    plan.require_recoverable()


def test_plan_recovery_submitted_but_zero_actual_reloads(tmp_path) -> None:
    """检查点说已提交、目标表实测 0 行（提交丢失）→ 重装（§9.6）。"""
    coord, staging, _store = _coordinator(tmp_path)
    coord.record_bulkload_result(_result("s0", "orders", 100))
    plan = coord.plan_recovery(lambda record: 0)
    actions = {a["shard_id"]: a["action"] for a in plan.actions}
    assert actions["s0"] == RecoveryAction.LOAD.value
    detail = next(a["detail"] for a in plan.actions if a["shard_id"] == "s0")
    assert "提交丢失" in detail


def test_plan_recovery_partial_offset_confirm(tmp_path) -> None:
    coord, staging, _store = _coordinator(tmp_path)
    coord.record_bulkload_result(_result("s0", "orders", 100))
    plan = coord.plan_recovery(lambda record: 60)  # 部分提交
    assert plan.partial_shards == ["s0"]
    actions = {a["shard_id"]: a["action"] for a in plan.actions}
    assert actions["s0"] == RecoveryAction.OFFSET_CONFIRM.value
    plan.require_recoverable()  # 部分提交不是不一致——分类处置


def test_plan_recovery_partial_cleanup_action(tmp_path) -> None:
    coord, staging, _store = _coordinator(tmp_path)
    coord.record_bulkload_result(_result("s0", "orders", 100))
    plan = coord.plan_recovery(lambda record: 60,
                               partial_action=RecoveryAction.CLEANUP.value)
    assert plan.shards_with_action(RecoveryAction.CLEANUP) == ["s0"]


def test_plan_recovery_invalid_partial_action_fails(tmp_path) -> None:
    coord, _staging, _store = _coordinator(tmp_path)
    with pytest.raises(CheckpointStoreError, match="cleanup/offset_confirm"):
        coord.plan_recovery(lambda record: 0, partial_action="reinstall")


def test_plan_recovery_missing_actual_rows_is_inconsistency(tmp_path) -> None:
    """§9.6：恢复前必须查实际写入量——缺失显式失败，不静默放行。"""
    coord, staging, _store = _coordinator(tmp_path)
    coord.record_bulkload_result(_result("s0", "orders", 100))
    plan = coord.plan_recovery(lambda record: None)
    with pytest.raises(CheckpointStoreError, match="actual_rows"):
        plan.require_recoverable()


def test_plan_recovery_staging_loaded_without_checkpoint_fails(tmp_path) -> None:
    """staging 说 loaded、DB 检查点缺失（不重复不遗漏锚点违规）→ 显式失败。"""
    staging = _staging(tmp_path)
    _loaded(staging, "s0", 100)  # 绕过 coordinator 直接标记
    coord = CheckpointCoordinator(staging, FakeStore(), INSTANCE_ID)
    plan = coord.plan_recovery(lambda record: 100)
    assert any("DB 检查点缺失" in i for i in plan.inconsistencies)
    with pytest.raises(CheckpointStoreError, match="不一致"):
        plan.require_recoverable()


def test_plan_recovery_generated_without_checkpoint_fails(tmp_path) -> None:
    staging = _staging(tmp_path)
    staging._records["s0"].status = ShardStatus.GENERATED.value
    store = FakeStore()  # 空：无检查点记录
    coord = CheckpointCoordinator(staging, store, INSTANCE_ID)
    plan = coord.plan_recovery(lambda record: 0)
    assert any("检查点缺失" in i for i in plan.inconsistencies)


def test_plan_recovery_partial_chunk_overrides_shard_submitted(tmp_path) -> None:
    """多子任务非全文件原子：shard 级 SUBMITTED 但 chunk 未满 → 显式失败。"""
    coord, staging, store = _coordinator(tmp_path)
    coord.record_bulkload_result(_result("s0", "orders", 100))
    coord.record_chunk("s0", 1, 60, 60)
    coord.record_chunk("s0", 2, 40, 10)  # chunk 2 仅提交 10/40
    plan = coord.plan_recovery(lambda record: 70)
    assert any("非全文件原子" in i for i in plan.inconsistencies)
    with pytest.raises(CheckpointStoreError):
        plan.require_recoverable()


def test_submitted_state_enum_locked() -> None:
    """§12.4 submitted_state 域锁死（勿改枚举值）。"""
    assert [s.value for s in SubmittedState] == [
        "PENDING", "PARTIAL", "SUBMITTED", "CONFIRMED", "CLEANED",
    ]
