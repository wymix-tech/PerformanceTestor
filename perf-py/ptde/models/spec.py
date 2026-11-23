"""规格与计划模型：DatasetSpec / GenerationPlan / ExecutionManifest（总方案 §6.2/§6.6/§12.4）。

注意：DatasetSpec 不含 max_rows 字段（A1：新管线无钳制，约束不可满足时显式失败）。
"""

from __future__ import annotations

import enum
import re
from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field, field_validator, model_validator

from ptde.models.base import VersionedModel
from ptde.models.profile import DistributionFit

__all__ = [
    "AutoTarget",
    "AutoTier",
    "DatasetSpec",
    "EntityPlan",
    "ExecutionManifest",
    "GenerationPlan",
    "IdRange",
    "ModelFingerprints",
    "PlanSource",
    "ProfileMode",
    "Reproducibility",
    "ResourceConstraints",
    "RowSource",
    "ShardPlanDef",
    "ShardSummary",
    "ShardingPlan",
    "SpecTargets",
    "StorageEstimate",
    "StorageTarget",
    "TimeRange",
    "WorkloadScenario",
    "WorkloadTarget",
]

# storage.max_total 形态校验（数值换算在 ptde/planner/targets.parse_storage_bytes）
_STORAGE_TARGET_RE = re.compile(r"(?i)\d+(\.\d+)?\s*(B|KB|MB|GB|TB|PB)")


class ProfileMode(enum.StrEnum):
    """画像模式：仅源码 / 仅统计 / 混合。"""

    SOURCE_ONLY = "source_only"
    STATS_ONLY = "stats_only"
    HYBRID = "hybrid"


class RowSource(enum.StrEnum):
    """每表规模来源标注（R3 落地：explicit/production/derived/estimated）。"""

    EXPLICIT = "explicit"
    PRODUCTION = "production"
    DERIVED = "derived"
    ESTIMATED = "estimated"


class PlanSource(enum.StrEnum):
    """规划产出来源。"""

    USER = "user"
    PLANNER = "planner"


class TimeRange(VersionedModel):
    """数据时间跨度（如订单时间窗）。"""

    start: str | None = None  # ISO 日期/日期时间
    end: str | None = None
    timezone: str | None = None


class StorageTarget(VersionedModel):
    """存储容量目标（§6.2：max_total "200GB" 形态，数值换算在 R3）。"""

    max_total: str | None = None  # 例："200GB"
    include_indexes: bool = True


class WorkloadScenario(VersionedModel):
    """压测场景及权重（R3 workload 分支收编 plan_pipeline）。"""

    id: str
    weight: float = 1.0
    endpoints: list[str] = Field(default_factory=list)  # METHOD path 列表


class WorkloadTarget(VersionedModel):
    """工作负载目标。"""

    scenarios: list[WorkloadScenario] = Field(default_factory=list)


class AutoTier(VersionedModel):
    """自动规划档位。"""

    name: str
    target_rows: dict[str, int] = Field(default_factory=dict)
    note: str | None = None


class AutoTarget(VersionedModel):
    """自动规划目标：基于画像、容量约束和用户限制给出多档方案。"""

    tiers: list[AutoTier] = Field(default_factory=list)


class ScaleFactorTarget(VersionedModel):
    """生产规模倍率目标（按生产基线放大，逐表独立倍率）。"""

    factors: dict[str, float] = Field(default_factory=dict)


class SpecTargets(VersionedModel):
    """可组合目标集（RowTarget/ScaleFactorTarget/StorageTarget/WorkloadTarget/AutoTarget）。

    互斥/冲突组合在模型层校验（R3 T3.1 落地）：
    - AutoTarget 与显式目标互斥（自动规划由画像与约束自主给档，组合语义冲突）；
    - 行数/倍率必须为正；workload 场景权重 ∈ (0,1] 且合计为 1；
    - storage.max_total 限 "数值 + 单位" 形态（换算在 planner.targets 落地）。
    """

    rows: dict[str, int] = Field(default_factory=dict)  # 表名 → 目标行数
    scale_factor: ScaleFactorTarget | None = None
    storage: StorageTarget | None = None
    workload: WorkloadTarget | None = None
    auto: AutoTarget | None = None

    @field_validator("rows")
    @classmethod
    def _rows_positive(cls, v: dict[str, int]) -> dict[str, int]:
        for table, rows in v.items():
            if rows <= 0:
                raise ValueError(f"表 {table} 目标行数必须为正数，得到 {rows}")
        return v

    @field_validator("scale_factor")
    @classmethod
    def _factors_positive(cls, v: ScaleFactorTarget | None) -> ScaleFactorTarget | None:
        if v is not None:
            for table, factor in v.factors.items():
                if factor <= 0:
                    raise ValueError(f"表 {table} 生产规模倍率必须为正数，得到 {factor}")
        return v

    @model_validator(mode="after")
    def _validate_combination(self) -> SpecTargets:
        if self.auto is not None and (
            self.rows or self.scale_factor is not None or self.workload is not None
        ):
            raise ValueError(
                "AutoTarget 与显式目标（rows/scale_factor/workload）互斥："
                "自动规划基于画像与容量约束自主给出多档方案，与显式目标组合语义冲突"
            )
        if self.workload is not None and self.workload.scenarios:
            for s in self.workload.scenarios:
                if not 0 < s.weight <= 1:
                    raise ValueError(
                        f"压测场景 {s.id} 权重必须在 (0, 1] 区间，得到 {s.weight}"
                    )
            total = sum(s.weight for s in self.workload.scenarios)
            if abs(total - 1.0) > 1e-6:
                raise ValueError(
                    f"压测场景权重合计必须为 1，当前合计 {total}"
                    f"（{', '.join(s.id for s in self.workload.scenarios)}）"
                )
        if self.storage is not None and self.storage.max_total is not None:
            m = _STORAGE_TARGET_RE.fullmatch(self.storage.max_total.strip())
            if m is None:
                raise ValueError(
                    f"storage.max_total 形态非法：{self.storage.max_total!r}"
                    "（应为 '数值 + 单位'，如 '200GB'，单位 B/KB/MB/GB/TB/PB）"
                )
        return self


class ResourceConstraints(VersionedModel):
    """资源约束（约束求解输入；不可满足 → PlanningConflict，R3 T3.2）。"""

    max_generation_time_ms: int | None = None
    max_memory_bytes: int | None = None
    max_staging_storage_bytes: int | None = None
    max_database_connections: int | None = None


class Reproducibility(VersionedModel):
    """可复现要素：Seed（QW1/R4 完整 Seed 链路的配置入口）。"""

    seed: int | None = None


class DatasetSpec(VersionedModel):
    """用户指定的测试数据目标（总方案 §6.2）。"""

    name: str
    profile_mode: ProfileMode = ProfileMode.HYBRID
    production_profile_id: str | None = None
    target_database: str | None = None  # 目标环境标识
    targets: SpecTargets = Field(default_factory=SpecTargets)
    time_range: TimeRange | None = None
    constraints: ResourceConstraints = Field(default_factory=ResourceConstraints)
    reproducibility: Reproducibility = Field(default_factory=Reproducibility)


class EntityPlan(VersionedModel):
    """单实体计划：规模 + 来源标注 + 推导说明（总方案 §6.6）。"""

    entity: str
    rows: int
    source: RowSource = RowSource.ESTIMATED
    derivation: str | None = None  # 推导说明（配额修正等事件记录于此）
    distribution: dict[str, Any] = Field(default_factory=dict)
    parent_binding: dict[str, Any] = Field(default_factory=dict)


class StorageEstimate(VersionedModel):
    """存储估算：Pilot Run 校准 B_i 产出（禁止字段长度相加，R3 T3.4）。"""

    estimated_table_size_bytes: int | None = None
    estimated_index_size_bytes: int | None = None
    estimated_total_size_bytes: int | None = None
    confidence: float | None = None
    error_band: str | None = None  # 误差区间说明
    calibrated: bool = False  # 是否经试装载校准


class DistributionPlanEntry(VersionedModel):
    """字段分布参数计划（R3 T3.6 产出，R4 采样器消费）。"""

    entity: str
    field: str
    distribution: DistributionFit | None = None
    source: str = "estimated"


class ShardPlanDef(VersionedModel):
    """分片计划定义（语言无关协议 §7.5 的计划侧雏形）。"""

    entity: str
    shard_id: str
    id_start: int | None = None
    id_end: int | None = None
    strategy: str = "planned_range"
    rows: int | None = None  # 本分片目标行数（hash_bucket 无 ID 区间时以此为准）


class ShardingPlan(VersionedModel):
    """分片计划（R4 T4.8 细化）。"""

    partition_algorithm_version: str = "1"
    shards: list[ShardPlanDef] = Field(default_factory=list)


class GenerationPlan(VersionedModel):
    """规模、分布、分片及执行计划。"""

    id: str
    dataset_spec_fingerprint: str
    target: str | None = None
    entities: dict[str, EntityPlan] = Field(default_factory=dict)
    storage: StorageEstimate | None = None
    distribution_plan: list[DistributionPlanEntry] = Field(default_factory=list)
    sharding_plan: ShardingPlan | None = None
    warnings: list[str] = Field(default_factory=list)  # 降级/推断告警
    conflict_errors: list[str] = Field(default_factory=list)  # 逐条冲突说明（显式失败）


class ModelFingerprints(VersionedModel):
    """ExecutionManifest 关联的模型指纹组（可复现追溯链）。

    序列化键沿用方案命名 `schema`；Python 侧字段名 `schema_fp`
    （避免遮蔽 pydantic BaseModel.schema 属性）。
    """

    model_config = ConfigDict(populate_by_name=True)

    spec: str | None = None
    plan: str | None = None
    profile: str | None = None
    schema_fp: str | None = Field(default=None, alias="schema")


class IdRange(VersionedModel):
    """实体实际 ID 区间（总方案 §12.4：实际生成结果回填）。"""

    start: int
    end: int
    strategy: str = "planned_range"


class ShardSummary(VersionedModel):
    """分片执行摘要（R4/R5 回填；语言无关协议结果侧）。"""

    shard_id: str
    entity: str
    generated_rows: int = 0
    loaded_rows: int | None = None
    checksum: str | None = None
    status: str = "pending"


class ExecutionManifest(VersionedModel):
    """每次生成和装载过程记录（总方案 §12.4 可复现要素全集）。"""

    exclude_fingerprint_fields = frozenset({"start_time", "end_time"})

    id: str
    instance_id: str | None = None
    seed: int | None = None
    generator_version: str = "0.0.0"
    partition_algorithm_version: str = "1"
    model_fingerprints: ModelFingerprints = Field(default_factory=ModelFingerprints)
    adapter_capability_snapshot: dict[str, Any] = Field(default_factory=dict)
    adapter_version: str | None = None
    time_inputs: dict[str, str] = Field(default_factory=dict)  # 时间输入固定值
    actual_id_ranges: dict[str, IdRange] = Field(default_factory=dict)
    shard_summaries: list[ShardSummary] = Field(default_factory=list)
    start_time: datetime | None = None
    end_time: datetime | None = None
    # P1-3 压测参数样本导出（READY 后生成；可选，缺省 None 不进指纹）
    sample_csv_path: str | None = None
    sample_csv_rows: int = 0
