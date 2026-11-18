"""依赖序装载编排器测试（B6 续收口；R5 §13 衔接项；实施方案 §8.2/§9.6）。

覆盖：无环依赖序装载、CycleResolution 四策略消费（NULLABLE_BACKFILL 两阶段
+ 回填 / STAGED_LOAD 显式批准 / DEFERRED_CONSTRAINT 事务边界 / SELF_REFERENCE）、
T5.7 检查点接入主流程（prepare → chunk → bulkload → recover 分类执行）、
staging 物理清理衔接（verify_complete + CLEANUP 授权 + 审计 + CLEANED）。
全部经 fake 连接/假仓本环境闭环（真库端到端随真库装载基准归档）。
"""

from __future__ import annotations

import pytest

from ptde.adapters.base import BulkLoadAdapter, BulkLoadError, BulkLoadResult
from ptde.adapters.bulkload.fallback import DbApiBatchInsertLoader, FallbackLoader
from ptde.adapters.mysql.statistics import StatisticsCollectionError
from ptde.generator.cycle_policy import CycleFkEdge, CycleStrategy, resolve_cycle
from ptde.generator.file_format import read_shard_file, write_shard_file
from ptde.generator.staging import StagingArea
from ptde.loader.checkpoint import (
    CheckpointCoordinator,
    CheckpointStoreError,
    SubmittedState,
)
from ptde.loader.gate import AuditLog, DestructiveApproval, GateDenied, SecurityGate
from ptde.loader.orchestrator import (
    BackfillStep,
    LoadOrchestrator,
    LoadOrchestratorError,
)

INSTANCE_ID = 7


# ============================================================ 测试桩


class FakeCursor:
    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql: str, params=()) -> None:
        self._conn.executed.append(("execute", sql, tuple(params)))

    def executemany(self, sql: str, batch) -> None:
        self._conn.executed.append(
            ("executemany", sql, tuple(tuple(r) for r in batch)),
        )

    def close(self) -> None:
        pass


class FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


class FakeStore:
    """检查点仓测试桩（接口对齐 DbCheckpointStore；字典后端确定性）。"""

    def __init__(self) -> None:
        self._rows: dict[tuple, object] = {}

    def upsert(self, cp) -> None:
        key = (cp.instance_id, cp.table_name, cp.shard_id, cp.chunk_id)
        self._rows[key] = cp.model_copy(deep=True)

    def get(self, instance_id, table_name, shard_id, chunk_id=0):
        row = self._rows.get((instance_id, table_name, shard_id, chunk_id))
        return row.model_copy(deep=True) if row else None

    def list_for_instance(self, instance_id, table_name=None):
        return [
            cp.model_copy(deep=True)
            for key, cp in sorted(self._rows.items())
            if key[0] == instance_id and (table_name is None or key[1] == table_name)
        ]


class FakeConstraintAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def disable_foreign_key_checks(self, conn) -> None:
        self.calls.append("disable")

    def enable_foreign_key_checks(self, conn) -> None:
        self.calls.append("enable")

    def list_trigger_names(self, conn, table) -> list[str]:
        return []


class FakeTransactionAdapter:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self, conn) -> None:
        self.commits += 1

    def rollback(self, conn) -> None:
        self.rollbacks += 1


class StubNativeLoader(BulkLoadAdapter):
    """单事务原生档替身（DEFERRED_CONSTRAINT 场景；不逐批 commit）。"""

    product = "mysql"
    strategy = "stub_native"

    def ensure_capable(self, conn) -> list[str]:
        return ["stub native"]

    def load_shard(self, conn, path, table, columns, rows_expected, *,
                   shard_id="", entity="", **_kw) -> BulkLoadResult:
        rows = sum(1 for _h, _r in read_shard_file(path))
        return BulkLoadResult(
            shard_id=shard_id, entity=entity, table=table,
            strategy=self.strategy, rows_expected=rows_expected,
            rows_loaded=rows, rows_verified=True,
        )


class FailingRuntimeLoader(BulkLoadAdapter):
    """运行时失败替身（BulkLoadError 原样抛出——A3 不静默换通道）。"""

    product = "mysql"
    strategy = "failing_runtime"

    def ensure_capable(self, conn) -> list[str]:
        return []

    def load_shard(self, conn, path, table, columns, rows_expected, *,
                   shard_id="", entity="", **_kw) -> BulkLoadResult:
        raise BulkLoadError(
            "注入的运行时失败", rows_expected=rows_expected, rows_loaded=0,
        )


# ============================================================ 构造工具


def _make_staging(tmp_path, plan) -> StagingArea:
    staging = StagingArea(tmp_path / "staging", plan_fingerprint="fp:test")
    for entity, shards, header in plan:
        for shard_id, rows in shards:
            path = staging.shard_path(entity, shard_id)
            meta = write_shard_file(path, header, rows)
            staging.register(entity, shard_id)
            staging.mark_generated(shard_id, meta)
    staging.checkpoint()
    return staging


def _batch_loader(batch_size: int = 2) -> FallbackLoader:
    return FallbackLoader(
        product="mysql",
        chain=[DbApiBatchInsertLoader(batch_size, product="mysql")],
    )


def _orchestrator(tmp_path, staging, *, loader=None, conn=None, **kw):
    conn = conn or FakeConn()
    store = FakeStore()
    coordinator = CheckpointCoordinator(staging, store, INSTANCE_ID)
    orch = LoadOrchestrator(
        conn, loader or _batch_loader(), coordinator, staging, **kw,
    )
    return orch, conn, coordinator, store


HEADER = ["id", "name", "peer_id"]


def _plan_rows(entity: str, n: int) -> list[list]:
    return [[i + 1, f"{entity}-{i + 1}", (i % 3) + 1] for i in range(n)]


CLEANUP_APPROVAL = DestructiveApproval(
    operation="CLEANUP", target="", authorized_by="tester", ticket="T-1",
)


def _backfill(entity: str) -> list[BackfillStep]:
    return [
        BackfillStep(
            table=entity, column="peer_id",
            sql=f"UPDATE {entity} SET peer_id = %s WHERE id = %s",
            params=[9, 1],
        ),
    ]


# ============================================================ 无环依赖序装载


def test_plain_topo_order_loads_and_submits(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("customers", [("c0", _plan_rows("c", 2))], HEADER),
        ("orders", [("o0", _plan_rows("o", 3))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"customers": HEADER, "orders": HEADER},
    )
    report = orch.run(["customers", "orders"])

    assert report.entities_loaded == ["customers", "orders"]
    assert report.shards_loaded == 2
    assert report.rows_loaded == 5
    assert staging.totals()["loaded"] == 2
    cps = store.list_for_instance(INSTANCE_ID)
    assert all(cp.submitted_state == SubmittedState.SUBMITTED.value for cp in cps)
    # manifest 落盘（检查点协议）
    assert (staging.root / "manifest.json").exists()


def test_parents_before_children_in_execution_order(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("child", [("k0", _plan_rows("k", 1))], HEADER),
        ("parent", [("p0", _plan_rows("p", 1))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"child": HEADER, "parent": HEADER},
    )
    orch.run(["parent", "child"])
    inserts = [e for e in conn.executed if e[0] == "executemany"]
    assert inserts[0][1].startswith("INSERT INTO parent")
    assert inserts[1][1].startswith("INSERT INTO child")


def test_prepare_checkpoints_registers_pending_then_run_submits(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 2))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    registered = orch.prepare_checkpoints()
    assert len(registered) == 1
    assert registered[0].submitted_state == SubmittedState.PENDING.value
    orch.run(["orders"])
    cp = store.get(INSTANCE_ID, "orders", "o0")
    assert cp.submitted_state == SubmittedState.SUBMITTED.value
    assert cp.loaded_rows == cp.expected_rows == 2


def test_chunk_records_via_on_batch_from_one(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 3))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"orders": HEADER}, loader=_batch_loader(batch_size=2),
    )
    orch.run(["orders"])
    chunks = [
        cp for cp in store.list_for_instance(INSTANCE_ID, "orders")
        if cp.chunk_id > 0
    ]
    assert [c.chunk_id for c in chunks] == [1, 2]  # chunk 从 1 起（chunk_id=0 汇总）
    assert chunks[0].expected_rows == 2 and chunks[1].expected_rows == 1
    assert chunks[0].loaded_rows == 2 and chunks[1].loaded_rows == 3  # 累计


def test_runtime_bulkload_error_propagates_no_channel_switch(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 2))], HEADER),
    ])
    loader = FallbackLoader(
        product="mysql",
        chain=[FailingRuntimeLoader(), DbApiBatchInsertLoader(2)],
    )
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER}, loader=loader,
    )
    with pytest.raises(BulkLoadError):
        orch.run(["orders"])
    # A3：运行时失败不静默换通道（降级链只对 BulkLoadUnsupported 生效）
    inserts = [e for e in conn.executed if e[0] == "executemany"]
    assert inserts == []


def test_duplicate_entity_in_order_explicit_fail(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 1))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    with pytest.raises(LoadOrchestratorError, match="重复"):
        orch.run(["orders", "orders"])


def test_unknown_entity_in_order_explicit_fail(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 1))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    with pytest.raises(LoadOrchestratorError, match="清单不一致"):
        orch.run(["ghost"])


def test_missing_entity_columns_explicit_fail(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 1))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={},
    )
    with pytest.raises(LoadOrchestratorError, match="列清单"):
        orch.run(["orders"])


# ============================================================ NULLABLE_BACKFILL 环


def _cycle_plan(tmp_path) -> StagingArea:
    return _make_staging(tmp_path, [
        ("a", [("a0", _plan_rows("a", 2))], HEADER),
        ("b", [("b0", _plan_rows("b", 2))], HEADER),
    ])


def _nullable_resolution() -> dict[tuple[str, ...], object]:
    edges = [
        CycleFkEdge(child_table="a", child_column="peer_id", nullable=True),
        CycleFkEdge(child_table="b", child_column="peer_id", nullable=True),
    ]
    return {("a", "b"): resolve_cycle(["a", "b"], edges)}


def test_nullable_backfill_phase1_nulls_and_backfill_runs(tmp_path) -> None:
    staging = _cycle_plan(tmp_path)
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"a": HEADER, "b": HEADER},
        cycle_columns={"a": ["peer_id"], "b": ["peer_id"]},
        backfill_provider=lambda entity: _backfill(entity),
    )
    report = orch.run([("a", "b")], _nullable_resolution())

    inserts = [e for e in conn.executed if e[0] == "executemany"]
    assert len(inserts) == 2
    peer_idx = HEADER.index("peer_id")
    for _kind, sql, batch in inserts:  # 阶段一：环内 FK 列为 NULL
        assert sql.startswith("INSERT INTO")
        for row in batch:
            assert row[peer_idx] is None

    updates = [e for e in conn.executed if e[0] == "execute"]
    assert [u[1] for u in updates] == [
        "UPDATE a SET peer_id = %s WHERE id = %s",
        "UPDATE b SET peer_id = %s WHERE id = %s",
    ]  # 阶段二：回填在装载后
    assert report.backfill_statements == 2
    # 回填 UPDATE 在全部分片装载之后（两阶段次序）
    kinds = [e[0] for e in conn.executed]
    assert kinds.index("execute") > max(
        i for i, k in enumerate(kinds) if k == "executemany"
    )
    assert conn.commits >= 1  # 回填后落库（loader 逐批 commit 之外另有编排器提交）
    assert report.strategies_used == [CycleStrategy.NULLABLE_BACKFILL.value]
    # 派生阶段一文件已清理
    assert not (staging.root / "_phase1").exists() or not any(
        (staging.root / "_phase1").rglob("*.csv")
    )


def test_cycle_without_resolution_explicit_fail(tmp_path) -> None:
    staging = _cycle_plan(tmp_path)
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"a": HEADER, "b": HEADER},
        cycle_columns={"a": ["peer_id"], "b": ["peer_id"]},
        backfill_provider=lambda entity: _backfill(entity),
    )
    with pytest.raises(LoadOrchestratorError, match="CycleResolution"):
        orch.run([("a", "b")], {})


def test_cycle_without_columns_projection_explicit_fail(tmp_path) -> None:
    staging = _cycle_plan(tmp_path)
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"a": HEADER, "b": HEADER},
        backfill_provider=lambda entity: _backfill(entity),
    )
    with pytest.raises(LoadOrchestratorError, match="回填列投影"):
        orch.run([("a", "b")], _nullable_resolution())


def test_cycle_member_without_projection_explicit_fail(tmp_path) -> None:
    staging = _cycle_plan(tmp_path)
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"a": HEADER, "b": HEADER},
        cycle_columns={"a": ["peer_id"]},
        backfill_provider=lambda entity: _backfill(entity),
    )
    with pytest.raises(LoadOrchestratorError, match="环成员 b"):
        orch.run([("a", "b")], _nullable_resolution())


def test_backfill_provider_missing_explicit_fail(tmp_path) -> None:
    staging = _cycle_plan(tmp_path)
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"a": HEADER, "b": HEADER},
        cycle_columns={"a": ["peer_id"], "b": ["peer_id"]},
    )
    with pytest.raises(LoadOrchestratorError, match="回填提供方"):
        orch.run([("a", "b")], _nullable_resolution())


def test_backfill_non_update_sql_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="UPDATE"):
        BackfillStep(
            table="a", column="peer_id",
            sql="DELETE FROM a WHERE id = %s", params=[1],
        )


def test_backfill_bad_identifier_rejected(tmp_path) -> None:
    with pytest.raises((ValueError, StatisticsCollectionError)):
        BackfillStep(
            table="a-bad", column="peer_id",
            sql="UPDATE a-bad SET peer_id = %s WHERE id = %s", params=[1],
        )


def test_backfill_sql_not_referencing_target_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="不一致"):
        BackfillStep(
            table="orders", column="peer_id",
            sql="UPDATE other SET peer_id = %s WHERE id = %s", params=[1],
        )


# ============================================================ STAGED_LOAD 环


def _staged_resolution() -> dict[tuple[str, ...], object]:
    edges = [
        CycleFkEdge(child_table="a", child_column="peer_id", nullable=False),
        CycleFkEdge(child_table="b", child_column="peer_id", nullable=False),
    ]
    return {("a", "b"): resolve_cycle(["a", "b"], edges,
                                      allow_constraint_disabled=True)}


def test_staged_load_without_approval_explicit_fail(tmp_path) -> None:
    staging = _cycle_plan(tmp_path)
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"a": HEADER, "b": HEADER},
        cycle_columns={"a": ["peer_id"], "b": ["peer_id"]},
        backfill_provider=lambda entity: _backfill(entity),
        constraint_adapter=FakeConstraintAdapter(),
        transaction_adapter=FakeTransactionAdapter(),
    )
    with pytest.raises(LoadOrchestratorError, match="显式批准"):
        orch.run([("a", "b")], _staged_resolution())


def test_staged_load_requires_adapters(tmp_path) -> None:
    staging = _cycle_plan(tmp_path)
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"a": HEADER, "b": HEADER},
        cycle_columns={"a": ["peer_id"], "b": ["peer_id"]},
        backfill_provider=lambda entity: _backfill(entity),
        allow_constraint_disabled=True,
    )
    with pytest.raises(LoadOrchestratorError, match="适配器"):
        orch.run([("a", "b")], _staged_resolution())


def test_staged_load_with_approval_disables_restores_and_backfills(tmp_path) -> None:
    staging = _cycle_plan(tmp_path)
    constraint = FakeConstraintAdapter()
    txn = FakeTransactionAdapter()
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"a": HEADER, "b": HEADER},
        cycle_columns={"a": ["peer_id"], "b": ["peer_id"]},
        backfill_provider=lambda entity: _backfill(entity),
        constraint_adapter=constraint,
        transaction_adapter=txn,
        allow_constraint_disabled=True,
    )
    report = orch.run([("a", "b")], _staged_resolution())

    assert constraint.calls == ["disable", "enable"]  # 退出恢复约束
    assert txn.commits == 1  # guard 提交
    peer_idx = HEADER.index("peer_id")
    inserts = [e for e in conn.executed if e[0] == "executemany"]
    for _kind, _sql, batch in inserts:
        for row in batch:  # 阶段一统一：环内 FK 列 NULL，回填在 guard 内完成
            assert row[peer_idx] is None
    assert report.backfill_statements == 2
    assert report.strategies_used == [CycleStrategy.STAGED_LOAD.value]


# ============================================================ DEFERRED_CONSTRAINT 环


def _deferred_resolution() -> dict[tuple[str, ...], object]:
    edges = [
        CycleFkEdge(child_table="a", child_column="peer_id", nullable=False),
        CycleFkEdge(child_table="b", child_column="peer_id", nullable=False),
    ]
    return {("a", "b"): resolve_cycle(["a", "b"], edges,
                                      supports_deferred_constraints=True)}


def test_deferred_constraint_rejects_batch_commit_loader(tmp_path) -> None:
    staging = _cycle_plan(tmp_path)
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"a": HEADER, "b": HEADER},
        cycle_columns={"a": ["peer_id"], "b": ["peer_id"]},
        backfill_provider=lambda entity: _backfill(entity),
        transaction_adapter=FakeTransactionAdapter(),
    )
    with pytest.raises(LoadOrchestratorError, match="逐批"):
        orch.run([("a", "b")], _deferred_resolution())


def test_deferred_constraint_commits_via_transaction_adapter(tmp_path) -> None:
    staging = _cycle_plan(tmp_path)
    txn = FakeTransactionAdapter()
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging,
        entity_columns={"a": HEADER, "b": HEADER},
        cycle_columns={"a": ["peer_id"], "b": ["peer_id"]},
        backfill_provider=lambda entity: _backfill(entity),
        transaction_adapter=txn,
        loader=FallbackLoader(product="mysql", chain=[StubNativeLoader()]),
    )
    report = orch.run([("a", "b")], _deferred_resolution())
    assert txn.commits == 1
    assert conn.commits == 0  # 单点提交走 TransactionAdapter（不经 conn.commit）
    assert report.rows_loaded == 4
    assert report.strategies_used == [CycleStrategy.DEFERRED_CONSTRAINT.value]


# ============================================================ SELF_REFERENCE 环


def test_self_reference_plain_load(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("nodes", [("n0", _plan_rows("n", 2))], HEADER),
    ])
    edges = [CycleFkEdge(child_table="nodes", child_column="peer_id",
                         nullable=False)]
    resolutions = {("nodes",): resolve_cycle(["nodes"], edges)}
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"nodes": HEADER},
    )
    report = orch.run([("nodes",)], resolutions)
    assert report.entities_loaded == ["nodes"]
    assert report.strategies_used == [CycleStrategy.SELF_REFERENCE.value]
    # 自引用不置 NULL：按原始分片数据装载
    inserts = [e for e in conn.executed if e[0] == "executemany"]
    assert all(row[HEADER.index("peer_id")] is not None
               for _k, _s, batch in inserts for row in batch)


# ============================================================ T5.7 恢复接入（§9.6）


def test_recover_load_action_reloads_generated_shard(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 2))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    orch.prepare_checkpoints()  # generated → DB PENDING
    report = orch.recover(lambda record: 0)
    assert report.shards_loaded == 1
    assert store.get(INSTANCE_ID, "orders", "o0").submitted_state == \
        SubmittedState.SUBMITTED.value


def test_recover_confirm_action_marks_confirmed(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 2))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    orch.prepare_checkpoints()
    orch.run(["orders"])
    orch.recover(lambda record: 2)  # 实测 == 期望 → CONFIRM
    assert store.get(INSTANCE_ID, "orders", "o0").submitted_state == \
        SubmittedState.CONFIRMED.value


def test_recover_generate_action_explicit_fail(tmp_path) -> None:
    staging = StagingArea(tmp_path / "staging", plan_fingerprint="fp")
    staging.register("orders", "o0")  # 仍 pending
    staging.checkpoint()
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    with pytest.raises(LoadOrchestratorError, match="pending"):
        orch.recover(lambda record: 0)


def test_recover_offset_confirm_explicit_fail_by_default(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 4))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    orch.prepare_checkpoints()
    orch.run(["orders"])
    with pytest.raises(LoadOrchestratorError, match="偏移确认"):
        orch.recover(lambda record: 2)  # 部分提交 → 默认 offset_confirm


def test_recover_inconsistent_plan_explicit_fail(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 2))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    # staging 已 generated 但 DB 检查点缺失（未 prepare）→ 不一致显式失败
    with pytest.raises(CheckpointStoreError):
        orch.recover(lambda record: 0)


def test_recover_cleanup_requires_gate(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 4))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    orch.prepare_checkpoints()
    orch.run(["orders"])
    called: list[str] = []
    with pytest.raises(LoadOrchestratorError, match="recovery_gate"):
        orch.recover(
            lambda record: 2, partial_action="cleanup",
            env_class="dedicated_test_db",
            approvals=[CLEANUP_APPROVAL],
            cleanup_executor=called.append,
        )
    assert called == []


def test_recover_cleanup_denied_without_approval(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 4))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
        recovery_gate=SecurityGate(AuditLog()),
    )
    orch.prepare_checkpoints()
    orch.run(["orders"])
    called: list[str] = []
    with pytest.raises(GateDenied):
        orch.recover(
            lambda record: 2, partial_action="cleanup",
            env_class="dedicated_test_db",
            approvals=[],  # 无 CLEANUP 授权
            cleanup_executor=called.append,
        )
    assert called == []


def test_recover_cleanup_with_approval_executes_and_reloads(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 4))], HEADER),
    ])
    gate = SecurityGate(AuditLog())
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
        recovery_gate=gate,
    )
    orch.prepare_checkpoints()
    orch.run(["orders"])
    called: list[str] = []
    report = orch.recover(
        lambda record: 2, partial_action="cleanup",
        env_class="dedicated_test_db",
        approvals=[CLEANUP_APPROVAL],
        cleanup_executor=called.append,
    )
    assert called == ["o0"]  # 部分行清理回调执行
    assert report.shards_loaded == 1  # 清理后重装
    cp = store.get(INSTANCE_ID, "orders", "o0")
    assert cp.submitted_state == SubmittedState.SUBMITTED.value
    assert cp.loaded_rows == 4  # 行数复核锚点恢复
    # CLEANUP 授权入审计
    assert any(e["event_type"] == "destructive_granted"
               for e in gate.audit.to_snapshot())


# ============================================================ staging 物理清理衔接


def test_purge_staging_incomplete_explicit_fail(tmp_path) -> None:
    staging = StagingArea(tmp_path / "staging", plan_fingerprint="fp")
    staging.register("orders", "o0")  # pending：不完整
    staging.checkpoint()
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    with pytest.raises(ValueError, match="不完整"):
        orch.purge_staging(
            env_class="dedicated_test_db",
            approvals=[CLEANUP_APPROVAL],
            gate=SecurityGate(AuditLog()),
        )


def test_purge_staging_requires_authorization(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 2))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    orch.run(["orders"])
    with pytest.raises(GateDenied):
        orch.purge_staging(
            env_class="dedicated_test_db",
            approvals=[],  # 无 CLEANUP 授权：文件不动
            gate=SecurityGate(AuditLog()),
        )
    assert staging.shard_path("orders", "o0").exists()


def test_purge_staging_production_env_denied(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 2))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    orch.run(["orders"])
    with pytest.raises(GateDenied):
        orch.purge_staging(
            env_class="production",  # §13.1：生产库禁止清理
            approvals=[CLEANUP_APPROVAL],
            gate=SecurityGate(AuditLog()),
        )
    assert staging.shard_path("orders", "o0").exists()


def test_purge_staging_success_deletes_and_marks_cleaned(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 2))], HEADER),
    ])
    gate = SecurityGate(AuditLog())
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    orch.prepare_checkpoints()
    orch.run(["orders"])
    result = orch.purge_staging(
        env_class="dedicated_test_db",
        approvals=[CLEANUP_APPROVAL],
        gate=gate,
    )
    assert not staging.root.exists()  # 物理删除已执行
    cps = [cp for cp in store.list_for_instance(INSTANCE_ID) if cp.chunk_id == 0]
    assert cps and all(
        cp.submitted_state == SubmittedState.CLEANED.value for cp in cps
    )
    events = [e["event_type"] for e in result["audit"]]
    assert "staging_purged" in events
    assert "destructive_granted" in events
    assert result["totals"]["rows_loaded"] == 2


# ============================================================ staging 重置协议


def test_staging_reset_to_generated_only_from_loaded(tmp_path) -> None:
    staging = _make_staging(tmp_path, [
        ("orders", [("o0", _plan_rows("o", 2))], HEADER),
    ])
    orch, conn, coordinator, store = _orchestrator(
        tmp_path, staging, entity_columns={"orders": HEADER},
    )
    with pytest.raises(ValueError, match="generated"):
        staging.reset_to_generated("o0")  # pending 不可重置
    orch.run(["orders"])
    record = staging.reset_to_generated("o0")  # loaded → generated（提交丢失）
    assert record.status == "generated"
    assert record.loaded_rows is None
    assert record.rows == 2 and record.checksum  # 锚点保留（重装后仍可复核）
