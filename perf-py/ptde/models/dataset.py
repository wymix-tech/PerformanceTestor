"""数据集生命周期模型：DatasetDefinition / DatasetInstance / ValidationReport + 状态机表。"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import Field

from ptde.models.base import DbRef, VersionedModel
from ptde.models.spec import DatasetSpec

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CheckResult",
    "DatasetDefinition",
    "DatasetInstance",
    "DatasetState",
    "LayerKind",
    "LayerResult",
    "LegacyRefs",
    "TargetEnvironment",
    "TransitionRecord",
    "ValidationReport",
    "ValidationStatus",
]


class DatasetState(enum.StrEnum):
    """数据集实例状态（总方案 §12.2）。"""

    PLANNED = "PLANNED"
    GENERATING = "GENERATING"
    LOADING = "LOADING"
    VERIFYING = "VERIFYING"
    READY = "READY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CLEANING = "CLEANING"
    CLEANED = "CLEANED"
    CANCELLED = "CANCELLED"


# 状态转换表（实施方案 §3.5，✔=允许；键为源状态，值为允许的目标状态集合）
ALLOWED_TRANSITIONS: dict[DatasetState, frozenset[DatasetState]] = {
    DatasetState.PLANNED: frozenset({DatasetState.GENERATING, DatasetState.CANCELLED}),
    DatasetState.GENERATING: frozenset({
        DatasetState.LOADING,
        DatasetState.PARTIAL,
        DatasetState.FAILED,
        DatasetState.CANCELLED,
    }),
    DatasetState.LOADING: frozenset({
        DatasetState.VERIFYING,
        DatasetState.PARTIAL,
        DatasetState.FAILED,
        DatasetState.CANCELLED,
    }),
    DatasetState.VERIFYING: frozenset({DatasetState.READY, DatasetState.FAILED}),
    DatasetState.PARTIAL: frozenset({DatasetState.GENERATING, DatasetState.LOADING}),
    DatasetState.FAILED: frozenset({DatasetState.CLEANING}),
    DatasetState.READY: frozenset({DatasetState.CLEANING}),
    DatasetState.CLEANING: frozenset({DatasetState.CLEANED}),
    # 终态：CLEANED / CANCELLED 无出边
    DatasetState.CLEANED: frozenset(),
    DatasetState.CANCELLED: frozenset(),
}


class TransitionRecord(VersionedModel):
    """状态转换记录：每次转换必须记录操作者/时间/原因（总方案 §12.2）。

    PARTIAL → GENERATING/LOADING 的恢复转换必须携带 manifest_id（上一次 Manifest）。
    """

    from_state: DatasetState
    to_state: DatasetState
    operator: str
    reason: str
    occurred_at: datetime
    manifest_id: str | None = None  # 恢复上下文（实施方案 §3.5 约束 3）


class TargetEnvironment(VersionedModel):
    """实例目标环境：DbRef + environment_id（实施方案 §3.4）。"""

    database: DbRef | None = None
    environment_id: str | None = None


class LegacyRefs(VersionedModel):
    """A4a 存量迁移映射（对齐 §12.1 ptde_dataset_instance.legacy_* 列）。"""

    legacy_plan_id: int | None = None  # 来源 data_gen_plan.id
    legacy_task_ids: list[int] = Field(default_factory=list)  # 来源 data_gen_task.id


class DatasetDefinition(VersionedModel):
    """可复用数据集定义：同一 def_key 多 version（def_key+version 唯一）。"""

    def_key: str
    version: str = "1"
    name: str
    spec: DatasetSpec
    spec_fingerprint: str | None = None
    created_by: str | None = None


class DatasetInstance(VersionedModel):
    """某目标环境中实际构建的数据集实例（状态机宿主）。"""

    exclude_fingerprint_fields = frozenset({"transition_log"})

    id: str
    definition_id: str
    definition_version: str = "1"
    plan_id: str | None = None
    target_environment: TargetEnvironment = Field(default_factory=TargetEnvironment)
    status: DatasetState = DatasetState.PLANNED
    transition_log: list[TransitionRecord] = Field(default_factory=list)
    actual_rows: int | None = None
    actual_storage_bytes: int | None = None
    validation_summary: dict | None = None
    owner_token: str | None = None  # 清理所有权标记（总方案 §12.5）
    legacy_refs: LegacyRefs | None = None
    error: str | None = None


class LayerKind(enum.StrEnum):
    """验证层（R6 五层验证）。"""

    COUNT = "count"
    RELATIONSHIP = "relationship"
    DISTRIBUTION = "distribution"
    BUSINESS = "business"
    DB_READY = "db_ready"


class ValidationStatus(enum.StrEnum):
    """验证结果：FAIL / WARN / PASS。"""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class CheckResult(VersionedModel):
    """单条检查结果。"""

    name: str
    status: ValidationStatus = ValidationStatus.PASS
    detail: str | None = None


class LayerResult(VersionedModel):
    """单层验证结果。"""

    layer: LayerKind
    status: ValidationStatus = ValidationStatus.PASS
    metrics: dict = Field(default_factory=dict)
    checks: list[CheckResult] = Field(default_factory=list)
    tolerance: dict | None = None
    evidence: list[str] = Field(default_factory=list)


class ValidationReport(VersionedModel):
    """数据集验证报告（五层）。"""

    instance_id: str
    layers: list[LayerResult] = Field(default_factory=list)
