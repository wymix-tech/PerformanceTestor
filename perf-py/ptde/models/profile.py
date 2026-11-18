"""统计画像模型：StatisticsProfile / UnifiedDataProfile（总方案 §4.4/§4.5、实施方案 §3.4）。

R2 产出；分布参数仅含统计特征（无明文业务值，脱敏防护见 R2 T2.7）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from ptde.models.base import VersionedModel

__all__ = [
    "ColumnProfile",
    "DistributionFit",
    "EntityProfile",
    "MergeConflict",
    "MergeDecision",
    "MergeReport",
    "ProfileSource",
    "RelationCardinalityProfile",
    "StatisticsProfile",
    "TableProfile",
    "TopKEntry",
    "UnifiedDataProfile",
]


class ProfileSource(VersionedModel):
    """画像来源：库只读采集 / 文件导入 / 测试库自画像（source_type 区分，R2 T2.4）。"""

    type: str  # db_readonly / file_upload / test_db_self
    database_type: str | None = None  # mysql / postgres
    collected_at: datetime | None = None
    sampling_rate: float | None = None
    accuracy: str = "estimated"  # 画像整体精度：exact/approximate/estimated（A3 分级）
    credential_label: str | None = None  # 所用凭据身份（脱敏标识，A5）


class DistributionFit(VersionedModel):
    """分布拟合结果：类型 + 参数（Uniform/Normal/LogNormal/Zipf/WeightedCategorical/Temporal…）。

    params 仅统计参数（均值/方差/形状/类别占比等），不含明文样本值。
    """

    type: str
    params: dict[str, Any] = Field(default_factory=dict)
    fitted: bool = False  # False = 显式配置或规则生成（标记 estimated，不得伪装为生产统计）


class TopKEntry(VersionedModel):
    """Top-K 频次条目（value 经分桶/哈希类别脱敏处理）。"""

    bucket: str  # 原值脱敏后的分桶标识或哈希类别
    count: int
    ratio: float


class LengthStats(VersionedModel):
    """字符串长度统计。"""

    min: int | None = None
    max: int | None = None
    avg: float | None = None


class ColumnProfile(VersionedModel):
    """列画像：基数/NULL/分布/Top-K/分位数/长度/拟合质量。"""

    exclude_fingerprint_fields = frozenset({"collected_at"})

    column: str
    distinct_count: int | None = None
    null_ratio: float | None = None
    accuracy: str = "estimated"  # 本列统计精度：exact/approximate/estimated/fitted（A3 分级）
    distribution: DistributionFit | None = None
    top_k: list[TopKEntry] = Field(default_factory=list)
    quantiles: dict[str, float] = Field(default_factory=dict)  # {"p50": ..., "p95": ...}
    length_stats: LengthStats | None = None
    fit_quality: float | None = None  # 拟合优度（如 1-KS 距离）
    collected_at: datetime | None = None


class TableProfile(VersionedModel):
    """表画像。"""

    table: str
    approximate_rows: int | None = None
    row_count_accuracy: str = "estimated"  # exact / approximate / estimated
    pk_columns: list[str] = Field(default_factory=list)  # 统计侧主键观察（不覆盖 Schema，R2 T2.6）
    columns: list[ColumnProfile] = Field(default_factory=list)
    relations: list[RelationCardinalityProfile] = Field(default_factory=list)


class StatisticsProfile(VersionedModel):
    """脱敏统计画像（单来源）。"""

    exclude_fingerprint_fields = frozenset({"collected_at"})

    id: str
    source: ProfileSource
    tables: list[TableProfile] = Field(default_factory=list)
    collected_at: datetime | None = None


class RelationCardinalityProfile(VersionedModel):
    """关联基数画像：父实体 → 子实体数量分布（如每用户订单数）。"""

    child_table: str
    parent_table: str
    child_per_parent_distribution: DistributionFit | None = None
    zero_child_ratio: float | None = None  # 零子实体父实体占比（R3 推导入模）


class EntityProfile(VersionedModel):
    """统一画像中的实体视图。"""

    entity: str
    table: str | None = None
    columns: dict[str, ColumnProfile] = Field(default_factory=dict)
    relation_cardinalities: list[RelationCardinalityProfile] = Field(default_factory=list)


class MergeDecision(VersionedModel):
    """融合裁决记录：某信息项以哪个来源为准（总方案 §4.4 优先级）。"""

    subject: str  # 字段/表/规模等裁决对象
    basis: str  # schema / source / stats / user_target / production_baseline ...
    chosen_from: str
    note: str | None = None


class MergeConflict(VersionedModel):
    """冲突清单：来源间不一致及裁决结果。"""

    subject: str
    sources: list[str] = Field(default_factory=list)
    winner: str
    reason: str


class MergeReport(VersionedModel):
    """融合报告：逐项裁决 + 冲突清单（硬约束不被统计覆盖）。"""

    decisions: list[MergeDecision] = Field(default_factory=list)
    conflicts: list[MergeConflict] = Field(default_factory=list)


class UnifiedDataProfile(VersionedModel):
    """融合后的统一数据画像。"""

    id: str
    sources: list[ProfileSource] = Field(default_factory=list)
    entities: dict[str, EntityProfile] = Field(default_factory=dict)
    merge_report: MergeReport | None = None
