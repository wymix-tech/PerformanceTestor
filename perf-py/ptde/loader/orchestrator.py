"""依赖序装载编排器（B6 续收口，R5 §13 衔接项；实施方案 §8.2/§9.6；总方案 §9.6）。

编排器本体（此前 R5 §13 记「协议两侧均已就绪，缺编排器本体」）：

- **依赖序装载**：按 ``CycleResolution`` 消费环安全策略（cycle_policy.py，
  勿另起判定）——无环实体按依赖序（父先于子）逐实体装载；
  环组按策略执行两阶段装载：
  - ``NULLABLE_BACKFILL``：阶段一分片装载（环内 FK 列置 NULL，生成侧契约），
    阶段二按回填提供方（BackfillStep 参数化 UPDATE）回填环内 FK 列；
  - ``DEFERRED_CONSTRAINT``：同两阶段，回填后经 TransactionAdapter 单点提交
    （逐批 commit 档与事务内延迟校验冲突，前置显式失败）；
  - ``STAGED_LOAD``：``staged_load_guard``（关闭 FK 校验须显式批准——
    总方案 §9.4/§19）内完成两阶段装载与回填，退出恢复约束并提交；
  - ``SELF_REFERENCE``：普通装载（深度上限语义在生成侧）。
- **T5.7 检查点接入装载主流程**：``prepare_checkpoints``（生成后登记
  PENDING）→ 每分片装载经 ``record_bulkload_result``（行数复核锚点）+
  ``record_chunk``（on_batch 回调，chunk 从 1 起）→ ``recover`` 消费
  ``plan_recovery`` 分类（§9.6：skip/confirm/load/cleanup/显式拒绝）；
- **staging 物理清理衔接**：``purge_staging``——``verify_complete`` 通过 +
  ``SecurityGate.evaluate_cleanup`` CLEANUP 显式授权（破坏性操作）后才执行
  物理删除（executor 回调注入），分片级检查点转 CLEANED 并入审计。

A3 口径延续：回填语句标识符白名单校验；staging 与编排清单不一致显式失败；
运行时装载失败（BulkLoadError）原样抛出不静默换通道。
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import Field

from ptde.adapters.base import BulkLoadResult
from ptde.adapters.bulkload.fallback import (
    DbApiBatchInsertLoader,
    FallbackLoader,
    staged_load_guard,
)
from ptde.adapters.mysql.statistics import validate_identifier
from ptde.generator.cycle_policy import CycleResolution, CycleStrategy
from ptde.generator.file_format import read_shard_file, write_shard_file
from ptde.generator.staging import StagingArea
from ptde.loader.checkpoint import (
    CheckpointCoordinator,
    RecoveryAction,
    ShardCheckpoint,
)
from ptde.loader.gate import DestructiveApproval, SecurityGate
from ptde_common.model_base import VersionedModel

__all__ = [
    "BackfillStep",
    "BackfillProvider",
    "LoadOrchestrator",
    "LoadOrchestratorError",
    "LoadReport",
]

# 回填提供方：entity → 参数化回填语句（生成侧映射接入缝，编排器不重推映射）
BackfillProvider = Callable[[str], Sequence["BackfillStep"]]

PHASE1_DIR = "_phase1"  # 环内 FK 置 NULL 的派生分片文件目录（staging.root 下）


class LoadOrchestratorError(Exception):
    """装载编排违规（清单/策略/授权不一致，显式失败不静默）。"""


class BackfillStep(VersionedModel):
    """单条参数化回填语句（阶段二；标识符白名单前置校验）。"""

    table: str
    column: str
    sql: str  # UPDATE 形态参数化语句（占位符 %s）
    params: list[Any] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        validate_identifier(self.table)
        validate_identifier(self.column)
        stripped = self.sql.strip()
        if not stripped.upper().startswith("UPDATE"):
            raise ValueError(
                f"回填语句须为 UPDATE（收到 {stripped[:12]!r}…，显式失败——"
                "回填通道不接受非 UPDATE 语句）"
            )
        if self.table not in stripped or self.column not in stripped:
            raise ValueError(
                f"回填语句未引用声明的 {self.table}.{self.column}（语句/声明不一致，显式失败）"
            )


class LoadReport(VersionedModel):
    """一次编排执行的汇总（可序列化，供 ExecutionManifest/审计引用）。"""

    instance_id: int
    entities_loaded: list[str] = Field(default_factory=list)
    shards_loaded: int = 0
    rows_loaded: int = 0
    backfill_statements: int = 0
    strategies_used: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LoadOrchestrator:
    """依赖序装载编排器（装载主流程 + T5.7 检查点 + staging 清理衔接）。"""

    def __init__(
        self,
        conn,
        loader: FallbackLoader,
        coordinator: CheckpointCoordinator,
        staging: StagingArea,
        *,
        entity_columns: dict[str, Sequence[str]],
        cycle_columns: dict[str, Sequence[str]] | None = None,
        backfill_provider: BackfillProvider | None = None,
        constraint_adapter: Any = None,
        transaction_adapter: Any = None,
        allow_constraint_disabled: bool = False,
        recovery_gate: SecurityGate | None = None,
    ) -> None:
        self._conn = conn
        self._loader = loader
        self._coordinator = coordinator
        self._staging = staging
        self._columns = {k: list(v) for k, v in entity_columns.items()}
        # 环内回填列投影：entity → 环内 FK 列（调用方由 project_fk_edges 投影）
        self._cycle_columns_map = {
            k: list(v) for k, v in (cycle_columns or {}).items()
        }
        self._backfill_provider = backfill_provider
        self._constraint_adapter = constraint_adapter
        self._transaction_adapter = transaction_adapter
        self._allow_constraint_disabled = allow_constraint_disabled
        self._recovery_gate_instance = recovery_gate
        self._loaded_entities: set[str] = set()

    # ------------------------------------------------------------ 入口

    def prepare_checkpoints(self) -> list[ShardCheckpoint]:
        """生成后登记检查点（staging generated → DB PENDING；已装载分片跳过）。"""
        registered = []
        for record in self._staging.records():
            if record.status == "generated":
                registered.append(self._coordinator.record_generation(record.shard_id))
        return registered

    def run(
        self,
        order: Sequence[str | Sequence[str]],
        resolutions: dict[tuple[str, ...], CycleResolution] | None = None,
    ) -> LoadReport:
        """按依赖序装载：str = 无环实体；Sequence = 环组（消费 CycleResolution）。"""
        report = LoadReport(instance_id=self._coordinator._instance_id)
        for step in order:
            if isinstance(step, str):
                self._run_plain(step, report)
            else:
                self._run_cycle(tuple(step), resolutions or {}, report)
        self._staging.checkpoint()  # 装载状态落盘（检查点协议原子写）
        return report

    # ------------------------------------------------------------ 恢复（§9.6）

    def recover(
        self,
        actual_rows_of: Callable[[Any], int | None],
        *,
        partial_action: str = RecoveryAction.OFFSET_CONFIRM.value,
        env_class: str = "",
        approvals: Sequence[DestructiveApproval] = (),
        cleanup_executor: Callable[[str], None] | None = None,
    ) -> LoadReport:
        """§9.6 恢复：消费 plan_recovery 分类并执行（cleanup 须过 CLEANUP 授权）。"""
        plan = self._coordinator.plan_recovery(
            actual_rows_of, partial_action=partial_action,
        )
        plan.require_recoverable()
        report = LoadReport(instance_id=self._coordinator._instance_id)
        for entry in plan.actions:
            action = entry["action"]
            shard_id = entry["shard_id"]
            entity = entry["entity"]
            if action == RecoveryAction.SKIP.value:
                continue
            if action == RecoveryAction.CONFIRM.value:
                self._coordinator.confirm(shard_id)
                continue
            if action == RecoveryAction.LOAD.value:
                self._reload_shard(shard_id, entity, report)
                continue
            if action == RecoveryAction.GENERATE.value:
                raise LoadOrchestratorError(
                    f"分片 {shard_id} 仍 pending（生成侧未完成）——编排器只做装载"
                    "编排不做生成（显式失败，不伪装可恢复）"
                )
            if action == RecoveryAction.OFFSET_CONFIRM.value:
                raise LoadOrchestratorError(
                    f"分片 {shard_id} 部分提交选择偏移确认——须人工/工具处置，"
                    "编排器不自动执行（显式失败）"
                )
            if action == RecoveryAction.CLEANUP.value:
                self._cleanup_partial_shard(
                    shard_id, entity, env_class, approvals, cleanup_executor, report,
                )
                continue
            raise LoadOrchestratorError(f"未知恢复动作 {action!r}（显式失败）")
        if report.shards_loaded:
            self._staging.checkpoint()
        return report

    # ------------------------------------------------------------ staging 物理清理

    def purge_staging(
        self,
        *,
        env_class: str,
        approvals: Sequence[DestructiveApproval],
        gate: SecurityGate,
        physical_executor: Callable[[Path], None] | None = None,
    ) -> dict[str, Any]:
        """staging 目录/分片文件物理删除（破坏性 CLEANUP：授权后才执行）。

        - ``verify_complete`` 不完整 → 显式失败（不完整数据不得清理）；
        - ``SecurityGate.evaluate_cleanup`` 拒绝 → GateDenied（文件不动）；
        - 物理删除动作经 ``physical_executor`` 注入（缺省 rmtree）；
        - 分片级检查点转 CLEANED + 审计记录。
        """
        totals = self._staging.verify_complete()
        gate.evaluate_cleanup(
            declared_env_class=env_class, approvals=approvals,
        ).require_passed()
        executor = physical_executor or (lambda root: shutil.rmtree(root))
        executor(self._staging.root)
        cleaned = self._coordinator.mark_all_cleaned()
        gate.audit.record(
            "staging_purged",
            root=str(self._staging.root),
            shards=totals["shards"],
            rows_loaded=totals["rows_loaded"],
            checkpoints_cleaned=len(cleaned),
        )
        return {"audit": gate.audit.to_snapshot(), "totals": totals}

    # ------------------------------------------------------------ 无环实体

    def _run_plain(self, entity: str, report: LoadReport) -> None:
        self._require_entity(entity)
        if entity in self._loaded_entities:
            raise LoadOrchestratorError(
                f"实体 {entity} 重复出现在装载序（不重复装载，显式失败）"
            )
        for record in self._records_of(entity):
            self._load_record(record, report, source_path=record.path)
        self._loaded_entities.add(entity)
        report.entities_loaded.append(entity)
        report.strategies_used.append("topological")

    def _run_cycle(
        self,
        members: tuple[str, ...],
        resolutions: dict[tuple[str, ...], CycleResolution],
        report: LoadReport,
    ) -> None:
        key = tuple(sorted(members))
        resolution = resolutions.get(key)
        if resolution is None:
            raise LoadOrchestratorError(
                f"环组 {key} 未提供 CycleResolution（安全策略判定不可跳过，显式失败）"
            )
        for member in members:
            self._require_entity(member)
            if member in self._loaded_entities:
                raise LoadOrchestratorError(
                    f"实体 {member} 已在装载序中出现（不重复装载，显式失败）"
                )

        if resolution.strategy == CycleStrategy.SELF_REFERENCE:
            # 自引用：普通装载（深度上限语义在生成侧；无需环列投影/回填）
            for member in members:
                for record in self._records_of(member):
                    self._load_record(record, report, source_path=record.path)
                self._loaded_entities.add(member)
                report.entities_loaded.append(member)
            report.strategies_used.append(CycleStrategy.SELF_REFERENCE.value)
            return

        columns_of = self._cycle_columns(key, members)

        # 两阶段装载（环组统一：阶段一 NULL 分片装载，阶段二回填）
        provider = self._require_backfill_provider(resolution)
        if resolution.strategy == CycleStrategy.DEFERRED_CONSTRAINT:
            self._reject_batch_commit_loader(key)
        if resolution.strategy == CycleStrategy.STAGED_LOAD:
            if not self._allow_constraint_disabled:
                raise LoadOrchestratorError(
                    f"环组 {key} 判定为 STAGED_LOAD：关闭 FK 校验须显式批准"
                    "（allow_constraint_disabled=False——默认保留全部约束，"
                    "总方案 §9.4/§19）"
                )
            if self._constraint_adapter is None or self._transaction_adapter is None:
                raise LoadOrchestratorError(
                    "STAGED_LOAD 需 Constraint/Transaction 适配器（执行原语缺失，显式失败）"
                )
            with staged_load_guard(
                self._conn, self._constraint_adapter, self._transaction_adapter,
                approved=True,
            ):
                self._two_phase_load(members, columns_of, provider, report,
                                     resolution.strategy.value)
            report.strategies_used.append(CycleStrategy.STAGED_LOAD.value)
            return

        self._two_phase_load(members, columns_of, provider, report,
                             resolution.strategy.value)
        if resolution.strategy == CycleStrategy.DEFERRED_CONSTRAINT:
            self._transaction_adapter.commit(self._conn)
        else:
            self._conn.commit()  # NULLABLE_BACKFILL：回填后落库
        report.strategies_used.append(resolution.strategy.value)

    def _two_phase_load(
        self,
        members: tuple[str, ...],
        columns_of: dict[str, list[str]],
        provider: BackfillProvider,
        report: LoadReport,
        strategy: str,
    ) -> None:
        # 阶段一：环内 FK 列置 NULL 装载（生成侧契约：环策略分片 FK 列为待回填位）
        for member in sorted(members):
            for record in self._records_of(member):
                phase1_path = self._write_phase1_shard(
                    record, columns_of.get(member, []),
                )
                self._load_record(record, report, source_path=phase1_path)
        # 阶段二：回填环内 FK 列（cycle_policy.steps 口径）
        for member in sorted(members):
            for bf in provider(member):
                cur = self._conn.cursor()
                try:
                    cur.execute(bf.sql, tuple(bf.params))
                finally:
                    cur.close()
                report.backfill_statements += 1
        for member in sorted(members):
            self._loaded_entities.add(member)
            report.entities_loaded.append(member)
        self._purge_phase1_files(members)

    # ------------------------------------------------------------ 分片装载

    def _load_record(
        self, record: Any, report: LoadReport, *, source_path: str,
    ) -> BulkLoadResult:
        if record.status != "generated":
            raise LoadOrchestratorError(
                f"分片 {record.shard_id} 状态 {record.status} ≠ generated，不可装载"
                "（已装载分片重复装载须走 recover/reset_to_generated，显式失败）"
            )
        columns = self._columns.get(record.entity)
        if columns is None:
            raise LoadOrchestratorError(
                f"实体 {record.entity} 未提供分片列清单（entity_columns，显式失败）"
            )
        outcome = self._loader.load_shard(
            self._conn,
            source_path,
            table=record.entity,
            columns=columns,
            rows_expected=record.rows,
            shard_id=record.shard_id,
            entity=record.entity,
            on_batch=(
                lambda chunk_id, batch_rows, rows_loaded, _sid=record.shard_id:
                self._coordinator.record_chunk(_sid, chunk_id, batch_rows, rows_loaded)
            ),
        )
        report.warnings.extend(outcome.warnings)
        report.shards_loaded += 1
        report.rows_loaded += outcome.result.rows_loaded
        self._coordinator.record_bulkload_result(outcome.result)  # 行数复核锚点
        return outcome.result

    def _reload_shard(self, shard_id: str, entity: str, report: LoadReport) -> None:
        record = self._require_shard(shard_id)
        if record.entity != entity:
            raise LoadOrchestratorError(
                f"分片 {shard_id} 实体 {record.entity} ≠ 恢复计划 {entity}（显式失败）"
            )
        if record.status == "loaded":
            # 提交丢失（DB 实测 0 行）：staging 退回可装载态（行数/校验和锚点保留）
            self._staging.reset_to_generated(shard_id)
            record = self._require_shard(shard_id)
        self._load_record(record, report, source_path=record.path)
        report.entities_loaded.append(entity)

    def _cleanup_partial_shard(
        self,
        shard_id: str,
        entity: str,
        env_class: str,
        approvals: Sequence[DestructiveApproval],
        cleanup_executor: Callable[[str], None] | None,
        report: LoadReport,
    ) -> None:
        if cleanup_executor is None:
            raise LoadOrchestratorError(
                f"分片 {shard_id} 部分提交选 cleanup：须注入 cleanup_executor"
                "（部分行 DB 清理回调，显式失败）"
            )
        gate = self._recovery_gate_instance
        if gate is None:
            raise LoadOrchestratorError(
                f"分片 {shard_id} 部分提交选 cleanup：未注入 recovery_gate"
                "（破坏性操作须过 SecurityGate CLEANUP 授权，显式失败）"
            )
        gate.evaluate_cleanup(
            declared_env_class=env_class, approvals=approvals,
        ).require_passed()
        cleanup_executor(shard_id)
        self._reload_shard(shard_id, entity, report)

    # ------------------------------------------------------------ 派生分片（阶段一）

    def _write_phase1_shard(self, record: Any, null_columns: Sequence[str]) -> str:
        """派生「阶段一」分片：环内 FK 列置 NULL（其余列/行数不变）。"""
        source = record.path
        if source is None:
            raise LoadOrchestratorError(
                f"分片 {record.shard_id} 未绑定分片文件路径（staging 元数据缺失，显式失败）"
            )
        reader = read_shard_file(source)
        first = next(reader, None)
        if first is None:
            raise LoadOrchestratorError(
                f"分片文件 {source} 无列头（格式违规，显式失败）"
            )
        header, _ = first
        indices: list[int] = []
        for col in null_columns:
            if col not in header:
                raise LoadOrchestratorError(
                    f"环内回填列 {col} 不在分片 {record.shard_id} 列头（显式失败）"
                )
            indices.append(header.index(col))

        def _rows():
            yield self._nulled(first[1], indices)
            for _h, row in reader:
                yield self._nulled(row, indices)

        phase1_path = self._staging.root / PHASE1_DIR / record.entity / (
            f"{record.shard_id}.csv"
        )
        write_shard_file(phase1_path, header, _rows())
        return str(phase1_path)

    @staticmethod
    def _nulled(row: list, indices: Sequence[int]) -> list:
        out = list(row)
        for i in indices:
            out[i] = None
        return out

    def _purge_phase1_files(self, members: tuple[str, ...]) -> None:
        """删除派生阶段一文件（本编排器自产中间产物，非 DB 破坏性操作）。"""
        phase1_root = self._staging.root / PHASE1_DIR
        for member in members:
            entity_dir = phase1_root / member
            if entity_dir.exists():
                shutil.rmtree(entity_dir)

    # ------------------------------------------------------------ 校验工具

    def _require_entity(self, entity: str) -> None:
        if not self._records_of(entity):
            raise LoadOrchestratorError(
                f"实体 {entity} 在 staging manifest 中无任何分片"
                "（清单不一致，显式失败）"
            )
        if self._columns.get(entity) is None:
            raise LoadOrchestratorError(
                f"实体 {entity} 未提供分片列清单（entity_columns，显式失败）"
            )

    def _require_shard(self, shard_id: str) -> Any:
        for record in self._staging.records():
            if record.shard_id == shard_id:
                return record
        raise LoadOrchestratorError(
            f"分片 {shard_id} 未在 staging manifest 登记（显式失败）"
        )

    def _records_of(self, entity: str) -> list[Any]:
        return [r for r in self._staging.records() if r.entity == entity]

    def _cycle_columns(
        self, key: tuple[str, ...], members: tuple[str, ...],
    ) -> dict[str, list[str]]:
        """环组回填列投影（构造时由 project_fk_edges 投影传入；缺失显式失败——A3）。"""
        if not self._cycle_columns_map:
            raise LoadOrchestratorError(
                f"环组 {key} 未提供环内回填列投影（cycle_columns：由 "
                "project_fk_edges 投影传入——可空性信息不足不得伪装可装载）"
            )
        out: dict[str, list[str]] = {}
        for member in members:
            cols = self._cycle_columns_map.get(member)
            if cols is None:
                raise LoadOrchestratorError(
                    f"环成员 {member} 缺少环内 FK 列投影（不得伪装可装载，显式失败）"
                )
            out[member] = list(cols)
        return out

    def _require_backfill_provider(self, resolution: CycleResolution) -> BackfillProvider:
        if resolution.requires_backfill or resolution.strategy == CycleStrategy.STAGED_LOAD:
            if self._backfill_provider is None:
                raise LoadOrchestratorError(
                    f"策略 {resolution.strategy.value} 需回填提供方（阶段二回填环内 "
                    "FK 列——backfill_provider 缺失显式失败，不伪装已回填）"
                )
            return self._backfill_provider
        raise LoadOrchestratorError(
            f"策略 {resolution.strategy.value} 无需回填提供方（显式失败）"
        )

    def _reject_batch_commit_loader(self, key: tuple[str, ...]) -> None:
        """DEFERRED_CONSTRAINT 与逐批 commit 档冲突：前置显式失败。"""
        chain = getattr(self._loader, "chain", [])
        for adapter in chain:
            if isinstance(adapter, DbApiBatchInsertLoader):
                raise LoadOrchestratorError(
                    f"环组 {key} 为 DEFERRED_CONSTRAINT：参数化批量 INSERT 档逐批 "
                    "commit 与事务内延迟校验冲突（须单事务装载通道，显式失败）"
                )

    @property
    def _recovery_gate(self) -> SecurityGate | None:
        """恢复 CLEANUP 动作的门禁（构造注入；未注入 → None，分支处显式失败）。"""
        return self._recovery_gate_instance
