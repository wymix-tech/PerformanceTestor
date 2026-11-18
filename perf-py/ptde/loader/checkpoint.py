"""分片检查点与断点恢复（R5 T5.7，实施方案 §8.2/§12.4；总方案 §9.6）。

协议口径（勿另起一套）：
- DB 检查点表 ``ptde_shard_checkpoint``（§12.4 DDL 原文）：每 Shard/Chunk
  记录期望行数/实际行数/校验摘要/提交状态（PENDING/PARTIAL/SUBMITTED/
  CONFIRMED/CLEANED）；
- **直接消费 staging manifest 协议**（``ptde/generator/staging.py``：
  ShardRecord 状态机 pending→generated→loaded、行数复核锚点）与
  ``BulkLoadResult.to_snapshot()``（db_feedback_json 原样落库，
  不重复不遗漏——§8.6 门禁 1/2）；
- 恢复流程对齐总方案 §9.6：读清单 → 查目标库实际写入量 → 区分
  已完成/未开始/部分提交 → 部分分片安全清理或偏移确认 → 重装并验证；
- **不能假设批量导入是全文件原子事务**（§9.6 末段）：多 chunk 子任务
  非全文件原子——恢复前核对实际提交状态（chunk 级记录 + actual_rows 实测）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import Field

from ptde.adapters.base import BulkLoadResult
from ptde_common.model_base import VersionedModel

__all__ = [
    "DDL_PTDE_SHARD_CHECKPOINT",
    "CheckpointCoordinator",
    "CheckpointStoreError",
    "DbCheckpointStore",
    "RecoveryAction",
    "RecoveryPlan",
    "ShardCheckpoint",
    "SubmittedState",
]

# §12.4 DDL 原文（MySQL 方言；幂等可重放）
DDL_PTDE_SHARD_CHECKPOINT = (
    "CREATE TABLE IF NOT EXISTS ptde_shard_checkpoint ("
    "  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,"
    "  instance_id BIGINT UNSIGNED NOT NULL,"
    "  table_name VARCHAR(128) NOT NULL,"
    "  shard_id VARCHAR(64) NOT NULL,"
    "  chunk_id INT NOT NULL DEFAULT 0,"
    "  expected_rows BIGINT NOT NULL,"
    "  generated_rows BIGINT NOT NULL DEFAULT 0,"
    "  loaded_rows BIGINT NOT NULL DEFAULT 0,"
    "  checksum CHAR(64) NULL COMMENT '分片文件 sha256',"
    "  last_chunk INT NOT NULL DEFAULT 0,"
    "  submitted_state VARCHAR(16) NOT NULL DEFAULT 'PENDING' "
    "COMMENT 'PENDING/PARTIAL/SUBMITTED/CONFIRMED/CLEANED',"
    "  db_feedback_json TEXT NULL COMMENT '数据库返回行数/警告/错误',"
    "  error_msg TEXT NULL,"
    "  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP "
    "ON UPDATE CURRENT_TIMESTAMP,"
    "  UNIQUE KEY uk_shard_chunk (instance_id, table_name, shard_id, chunk_id),"
    "  KEY idx_instance_status (instance_id, submitted_state)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='PTDE 分片检查点'"
)


class SubmittedState(StrEnum):
    """提交状态（§12.4 submitted_state 域，勿改枚举值）。"""

    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    CLEANED = "CLEANED"


class CheckpointStoreError(Exception):
    """检查点存取/状态机违规（显式失败，不静默）。"""


class ShardCheckpoint(VersionedModel):
    """§12.4 单 Shard/Chunk 检查点记录（字段对齐 DDL 列，snake_case）。"""

    instance_id: int
    table_name: str
    shard_id: str
    chunk_id: int = 0
    expected_rows: int = 0
    generated_rows: int = 0
    loaded_rows: int = 0
    checksum: str | None = None
    last_chunk: int = 0
    submitted_state: str = SubmittedState.PENDING.value
    db_feedback: dict[str, Any] | None = None  # BulkLoadResult.to_snapshot()
    error_msg: str | None = None


class DbCheckpointStore:
    """DB 检查点仓（§12.4 表；upsert 幂等可重放——重试不重复计数）。"""

    _COLUMNS = (
        "instance_id, table_name, shard_id, chunk_id, expected_rows, "
        "generated_rows, loaded_rows, checksum, last_chunk, "
        "submitted_state, db_feedback_json, error_msg"
    )

    def __init__(self, conn, *, commit: bool = True) -> None:
        self._conn = conn
        self._commit = commit
        self._ensure_table()

    def _ensure_table(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(DDL_PTDE_SHARD_CHECKPOINT)
        finally:
            cur.close()
        if self._commit:
            self._conn.commit()

    def upsert(self, cp: ShardCheckpoint) -> None:
        placeholders = ", ".join(["%s"] * 12)
        updates = ", ".join(
            f"{c} = VALUES({c})"
            for c in (
                "expected_rows", "generated_rows", "loaded_rows", "checksum",
                "last_chunk", "submitted_state", "db_feedback_json", "error_msg",
            )
        )
        sql = (
            f"INSERT INTO ptde_shard_checkpoint ({self._COLUMNS}) "
            f"VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {updates}"
        )
        cur = self._conn.cursor()
        try:
            cur.execute(sql, (
                cp.instance_id, cp.table_name, cp.shard_id, cp.chunk_id,
                cp.expected_rows, cp.generated_rows, cp.loaded_rows,
                cp.checksum, cp.last_chunk, cp.submitted_state,
                json.dumps(cp.db_feedback, ensure_ascii=False)
                if cp.db_feedback is not None else None,
                cp.error_msg,
            ))
        finally:
            cur.close()
        if self._commit:
            self._conn.commit()

    def get(
        self, instance_id: int, table_name: str, shard_id: str, chunk_id: int = 0
    ) -> ShardCheckpoint | None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT instance_id, table_name, shard_id, chunk_id, expected_rows, "
                "generated_rows, loaded_rows, checksum, last_chunk, "
                "submitted_state, db_feedback_json, error_msg "
                "FROM ptde_shard_checkpoint "
                "WHERE instance_id = %s AND table_name = %s AND shard_id = %s "
                "AND chunk_id = %s",
                (instance_id, table_name, shard_id, chunk_id),
            )
            row = cur.fetchone()
        finally:
            cur.close()
        return self._row_to_cp(row) if row else None

    def list_for_instance(
        self, instance_id: int, table_name: str | None = None
    ) -> list[ShardCheckpoint]:
        cur = self._conn.cursor()
        try:
            if table_name is None:
                cur.execute(
                    "SELECT instance_id, table_name, shard_id, chunk_id, expected_rows, "
                    "generated_rows, loaded_rows, checksum, last_chunk, "
                    "submitted_state, db_feedback_json, error_msg "
                    "FROM ptde_shard_checkpoint WHERE instance_id = %s "
                    "ORDER BY table_name, shard_id, chunk_id",
                    (instance_id,),
                )
            else:
                cur.execute(
                    "SELECT instance_id, table_name, shard_id, chunk_id, expected_rows, "
                    "generated_rows, loaded_rows, checksum, last_chunk, "
                    "submitted_state, db_feedback_json, error_msg "
                    "FROM ptde_shard_checkpoint "
                    "WHERE instance_id = %s AND table_name = %s "
                    "ORDER BY table_name, shard_id, chunk_id",
                    (instance_id, table_name),
                )
            rows = list(cur.fetchall())
        finally:
            cur.close()
        return [self._row_to_cp(r) for r in rows]

    @staticmethod
    def _row_to_cp(row: tuple) -> ShardCheckpoint:
        return ShardCheckpoint(
            instance_id=int(row[0]),
            table_name=row[1],
            shard_id=row[2],
            chunk_id=int(row[3]),
            expected_rows=int(row[4]),
            generated_rows=int(row[5]),
            loaded_rows=int(row[6]),
            checksum=row[7],
            last_chunk=int(row[8]),
            submitted_state=row[9],
            db_feedback=json.loads(row[10]) if row[10] else None,
            error_msg=row[11],
        )


class RecoveryAction(StrEnum):
    """§9.6 恢复分类动作。"""

    SKIP = "skip"  # 已 CONFIRMED 且实测一致
    CONFIRM = "confirm"  # SUBMITTED 且实测一致 → 走 CONFIRMED 核对
    LOAD = "load"  # 未装载（actual=0）
    GENERATE = "generate"  # staging 仍 pending
    CLEANUP = "cleanup"  # 部分提交 → 安全清理后重装（破坏性，须授权）
    OFFSET_CONFIRM = "offset_confirm"  # 部分提交 → 偏移确认（人工/工具）


class RecoveryPlan(VersionedModel):
    """§9.6 恢复计划（分类留痕；不一致项显式失败不静默放行）。"""

    instance_id: int
    actions: list[dict[str, str]] = Field(default_factory=list)
    inconsistencies: list[str] = Field(default_factory=list)
    partial_shards: list[str] = Field(default_factory=list)

    def require_recoverable(self) -> None:
        """存在清单/实际/检查点不一致 → 显式失败（不重复不遗漏的核对锚点）。"""
        if self.inconsistencies:
            raise CheckpointStoreError(
                f"恢复前核对发现不一致（{len(self.inconsistencies)} 项，显式失败）："
                + "；".join(self.inconsistencies)
            )

    def shards_with_action(self, action: RecoveryAction) -> list[str]:
        return [
            a["shard_id"] for a in self.actions if a["action"] == action.value
        ]


class CheckpointCoordinator:
    """staging manifest ↔ DB 检查点双向核对锚点（§8.6 门禁 1/2）。

    - ``record_generation``：staging generated → DB PENDING（期望行数/校验和）；
    - ``record_bulkload_result``：消费 ``BulkLoadResult`` 快照，行数复核锚点
      （staging.mark_loaded 与 DB SUBMITTED 同步——不重复不遗漏）；
    - ``record_chunk``：chunk 级进度（多子任务非全文件原子，恢复前核对）；
    - ``plan_recovery``：§9.6 恢复流程（分类 + 不一致显式失败）。
    """

    def __init__(
        self,
        staging: Any,  # ptde.generator.staging.StagingArea
        store: DbCheckpointStore,
        instance_id: int,
    ) -> None:
        self._staging = staging
        self._store = store
        self._instance_id = instance_id

    # ------------------------------------------------------------ 记录

    def _record(self, shard_id: str):
        for record in self._staging.records():
            if record.shard_id == shard_id:
                return record
        raise CheckpointStoreError(
            f"分片 {shard_id} 未在 staging manifest 登记（显式失败）"
        )

    def record_generation(self, shard_id: str) -> ShardCheckpoint:
        record = self._record(shard_id)
        if record.status != "generated":
            raise CheckpointStoreError(
                f"分片 {shard_id} 状态 {record.status} ≠ generated"
                "（先完成生成再登记检查点，显式失败）"
            )
        cp = ShardCheckpoint(
            instance_id=self._instance_id,
            table_name=record.entity,
            shard_id=shard_id,
            expected_rows=record.rows,
            generated_rows=record.rows,
            checksum=record.checksum,
            submitted_state=SubmittedState.PENDING.value,
        )
        self._store.upsert(cp)
        return cp

    def record_bulkload_result(self, result: BulkLoadResult) -> ShardCheckpoint:
        """消费装载结果：行数复核（staging 锚点）+ DB SUBMITTED + 快照落库。"""
        record = self._record(result.shard_id)
        if result.rows_loaded != record.rows:
            raise CheckpointStoreError(
                f"分片 {result.shard_id} 装载行数 {result.rows_loaded} "
                f"≠ 生成行数 {record.rows}（行数复核锚点不一致，显式失败）"
            )
        self._staging.mark_loaded(result.shard_id, result.rows_loaded)
        cp = ShardCheckpoint(
            instance_id=self._instance_id,
            table_name=record.entity,
            shard_id=result.shard_id,
            expected_rows=record.rows,
            generated_rows=record.rows,
            loaded_rows=result.rows_loaded,
            checksum=record.checksum,
            last_chunk=0,
            submitted_state=SubmittedState.SUBMITTED.value,
            db_feedback=result.to_snapshot(),  # 协议 §7.5 JSON 原生，不重复不遗漏
        )
        self._store.upsert(cp)
        return cp

    def record_chunk(
        self, shard_id: str, chunk_id: int, chunk_rows: int, rows_loaded: int
    ) -> ShardCheckpoint:
        """chunk 级进度登记（on_batch 回调；多子任务非全文件原子的核对输入）。"""
        record = self._record(shard_id)
        cp = ShardCheckpoint(
            instance_id=self._instance_id,
            table_name=record.entity,
            shard_id=shard_id,
            chunk_id=chunk_id,
            expected_rows=chunk_rows,
            generated_rows=chunk_rows,
            loaded_rows=rows_loaded,
            submitted_state=SubmittedState.SUBMITTED.value,
        )
        self._store.upsert(cp)
        return cp

    def confirm(self, shard_id: str) -> ShardCheckpoint:
        """SUBMITTED 且实测一致 → CONFIRMED（§9.6 恢复分类 CONFIRM 的落位）。"""
        record = self._record(shard_id)
        cp = self._store.get(self._instance_id, record.entity, shard_id)
        if cp is None or cp.chunk_id != 0:
            raise CheckpointStoreError(
                f"分片 {shard_id} 无 shard 级检查点（chunk_id=0 汇总行，显式失败）"
            )
        if cp.submitted_state != SubmittedState.SUBMITTED.value:
            raise CheckpointStoreError(
                f"分片 {shard_id} 状态 {cp.submitted_state} ≠ SUBMITTED"
                "（CONFIRMED 核对须先提交，显式失败）"
            )
        cp.submitted_state = SubmittedState.CONFIRMED.value
        self._store.upsert(cp)
        return cp

    def mark_all_cleaned(self) -> list[ShardCheckpoint]:
        """staging 物理清理后分片级检查点转 CLEANED（§12.4 CLEANED 状态语义）。"""
        cleaned: list[ShardCheckpoint] = []
        for cp in self._store.list_for_instance(self._instance_id):
            if cp.chunk_id != 0:
                continue
            if cp.submitted_state not in (
                SubmittedState.SUBMITTED.value, SubmittedState.CONFIRMED.value,
            ):
                raise CheckpointStoreError(
                    f"分片 {cp.shard_id} 状态 {cp.submitted_state} 不可转 CLEANED"
                    "（仅 SUBMITTED/CONFIRMED 可清理，显式失败）"
                )
            cp.submitted_state = SubmittedState.CLEANED.value
            self._store.upsert(cp)
            cleaned.append(cp)
        return cleaned

    # ------------------------------------------------------------ 恢复（总方案 §9.6）

    def plan_recovery(
        self,
        actual_rows_of: Callable[[Any], int | None],
        *,
        partial_action: str = RecoveryAction.OFFSET_CONFIRM.value,
    ) -> RecoveryPlan:
        """读清单 → 查实际 → 分类 → 不一致显式失败。

        partial_action：部分提交分片的处置（cleanup=安全清理后重装，属破坏性
        操作须过 T5.9 门禁授权；offset_confirm=偏移确认后续装）。
        """
        if partial_action not in (RecoveryAction.CLEANUP.value,
                                  RecoveryAction.OFFSET_CONFIRM.value):
            raise CheckpointStoreError(
                f"partial_action 仅支持 cleanup/offset_confirm，收到 {partial_action!r}"
                "（显式失败）"
            )
        plan = RecoveryPlan(instance_id=self._instance_id)
        for record in self._staging.records():
            cp = self._store.get(self._instance_id, record.entity, record.shard_id)
            actual = actual_rows_of(record)
            self._classify(record, cp, actual, partial_action, plan)
        return plan

    def _classify(
        self,
        record: Any,
        cp: ShardCheckpoint | None,
        actual: int | None,
        partial_action: str,
        plan: RecoveryPlan,
    ) -> None:
        shard_id = record.shard_id

        def action(kind: RecoveryAction, detail: str) -> None:
            plan.actions.append({
                "shard_id": shard_id,
                "entity": record.entity,
                "action": kind.value,
                "detail": detail,
            })

        # staging 与 DB 检查点清单不一致（§9.6 核对锚点，显式失败）
        if record.status == "loaded" and cp is None:
            plan.inconsistencies.append(
                f"{shard_id}: staging 已 loaded 但 DB 检查点缺失（不重复不遗漏锚点）"
            )
            return
        if record.status == "generated" and cp is None:
            plan.inconsistencies.append(
                f"{shard_id}: staging 已 generated 但 DB 检查点缺失"
                "（生成/检查点登记顺序违规）"
            )
            return

        if record.status == "pending":
            action(RecoveryAction.GENERATE, "staging 仍 pending（§9.6 未开始）")
            return
        if record.status == "generated":
            action(RecoveryAction.LOAD, "已生成未装载（§9.6 未开始）")
            return

        # loaded：核对实际提交状态（多 chunk 非全文件原子）
        actual_str = "未实测" if actual is None else str(actual)
        if actual is None:
            plan.inconsistencies.append(
                f"{shard_id}: 装载分片未提供 actual_rows 实测值"
                "（§9.6 恢复前必须查实际写入量，显式失败）"
            )
            return
        if cp is None:  # 上面已拦 loaded+cp None，此处防御
            plan.inconsistencies.append(f"{shard_id}: DB 检查点缺失")
            return
        if cp.chunk_id > 0:
            plan.inconsistencies.append(
                f"{shard_id}: shard 级记录 chunk_id={cp.chunk_id}≠0（应为 chunk 0 汇总行）"
            )
            return
        chunks = self._store.list_for_instance(self._instance_id, record.entity)
        chunk_rows = [
            c for c in chunks
            if c.shard_id == shard_id and c.chunk_id > 0
        ]
        partial_chunks = [
            c for c in chunk_rows if c.loaded_rows < c.expected_rows
        ]
        if partial_chunks:
            plan.inconsistencies.append(
                f"{shard_id}: shard 级 SUBMITTED 但存在未提交满的 chunk "
                f"{[c.chunk_id for c in partial_chunks]}"
                "（多子任务非全文件原子——§9.6，实际提交状态以 chunk 为准）"
            )
            return
        if actual == cp.expected_rows:
            action(RecoveryAction.CONFIRM,
                   f"实际写入 {actual_str} == 期望 {cp.expected_rows}（核对通过）")
            return
        if actual == 0:
            action(RecoveryAction.LOAD,
                   "检查点记录已提交但目标表实测 0 行（提交丢失，重装）")
            return
        # 0 < actual < expected：部分提交
        plan.partial_shards.append(shard_id)
        action(
            RecoveryAction(partial_action),
            f"部分提交：实际 {actual_str} / 期望 {cp.expected_rows}（§9.6 部分分片处置）",
        )
