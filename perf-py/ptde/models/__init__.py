"""PTDE 统一数据模型（总方案 §5.1 / 实施方案 §3.4）。

全部模型为 pydantic v2（A6），继承 VersionedModel（version + fingerprint），
支持 JSON/YAML 双读双写；不依赖任何方言/数据库驱动模块。
"""

from ptde.models.access import AccessPattern, JoinRef, LimitOffset, Predicate, SqlKind
from ptde.models.base import DbRef, Evidence, EvidenceStatus, VersionedModel
from ptde.models.dataset import (
    ALLOWED_TRANSITIONS,
    CheckResult,
    DatasetDefinition,
    DatasetInstance,
    DatasetState,
    LayerKind,
    LayerResult,
    LegacyRefs,
    TargetEnvironment,
    TransitionRecord,
    ValidationReport,
    ValidationStatus,
)
from ptde.models.entity import (
    BusinessInvariant,
    Cardinality,
    EntityModel,
    FieldModel,
    InvariantPredicate,
    RelationKind,
    RelationModel,
    RelationRef,
)
from ptde.models.profile import (
    ColumnProfile,
    DistributionFit,
    EntityProfile,
    MergeConflict,
    MergeDecision,
    MergeReport,
    ProfileSource,
    StatisticsProfile,
    TableProfile,
    TopKEntry,
    UnifiedDataProfile,
)
from ptde.models.snapshot import (
    CheckConstraintSchema,
    ColumnSchema,
    DatabaseSchemaSnapshot,
    DependencyRef,
    ForeignKeySchema,
    GeneratedColumnSchema,
    IndexColumn,
    IndexSchema,
    PartitionSchema,
    ProjectSnapshot,
    SequenceSchema,
    TableSchema,
    TechStackEntry,
    TriggerSchema,
    UniqueConstraintSchema,
)
from ptde.models.spec import (
    AutoTarget,
    AutoTier,
    DatasetSpec,
    EntityPlan,
    ExecutionManifest,
    GenerationPlan,
    IdRange,
    ModelFingerprints,
    PlanSource,
    ProfileMode,
    Reproducibility,
    ResourceConstraints,
    RowSource,
    ShardingPlan,
    ShardPlanDef,
    ShardSummary,
    SpecTargets,
    StorageEstimate,
    StorageTarget,
    TimeRange,
    WorkloadScenario,
    WorkloadTarget,
)

__all__ = [
    # base
    "DbRef", "Evidence", "EvidenceStatus", "VersionedModel",
    # snapshot
    "CheckConstraintSchema", "ColumnSchema", "DatabaseSchemaSnapshot", "DependencyRef",
    "ForeignKeySchema", "GeneratedColumnSchema", "IndexColumn", "IndexSchema",
    "PartitionSchema", "ProjectSnapshot", "SequenceSchema", "TableSchema",
    "TechStackEntry", "TriggerSchema", "UniqueConstraintSchema",
    # entity
    "BusinessInvariant", "Cardinality", "EntityModel", "FieldModel",
    "InvariantPredicate", "RelationKind", "RelationModel", "RelationRef",
    # access
    "AccessPattern", "JoinRef", "LimitOffset", "Predicate", "SqlKind",
    # profile
    "ColumnProfile", "DistributionFit", "EntityProfile", "LengthStats",
    "MergeConflict", "MergeDecision", "MergeReport", "ProfileSource",
    "RelationCardinalityProfile", "StatisticsProfile", "TableProfile",
    "TopKEntry", "UnifiedDataProfile",
    # spec
    "AutoTarget", "AutoTier", "DatasetSpec", "EntityPlan", "ExecutionManifest",
    "GenerationPlan", "IdRange", "ModelFingerprints", "ProfileMode", "PlanSource",
    "Reproducibility", "ResourceConstraints", "RowSource", "ShardPlanDef",
    "ShardSummary", "ShardingPlan", "SpecTargets", "StorageEstimate", "StorageTarget",
    "TimeRange", "WorkloadScenario", "WorkloadTarget",
    # dataset
    "ALLOWED_TRANSITIONS", "CheckResult", "DatasetDefinition", "DatasetInstance",
    "DatasetState", "LayerKind", "LayerResult", "LegacyRefs", "TargetEnvironment",
    "TransitionRecord", "ValidationReport", "ValidationStatus",
]
