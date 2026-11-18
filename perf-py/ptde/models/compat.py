"""旧→新模型适配层（A6 / F4）：legacy dataclass 不改造，只写适配函数。

- 新形态 `*V2` 为 pydantic v2 镜像模型（带 version+fingerprint，JSON/YAML 双读），
  逐字段承接 legacy dataclass 语义；
- `*_to_v2` / `*_from_v2` 双向映射：旧→新→旧往返字段无丢失（单测覆盖）；
- 安全例外：`DataGenSpec.db_password` 不进入模型（凭据由连接层管理，不落模型/指纹）；
- `DataGenSpecV2.max_rows` 为 legacy A1 钳制口径的兼容承载——仅 legacy 对照管线使用，
  新 PTDE 管线的 DatasetSpec 无此字段（分轨，见实施方案 §2.3 F1）。

R7 随 datagen 包退役时本模块一并清理。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from datagen.model import (
    ColumnMeta,
    DataGenSpec,
    FieldRule,
    FkMeta,
    OverThreshold,
    RelationHint,
    SchemaMetadata,
    TableSpec,
)
from ptde_common.model_base import VersionedModel

__all__ = [
    "ColumnMetaV2",
    "DataGenSpecV2",
    "FieldRuleV2",
    "FkMetaV2",
    "OverThresholdV2",
    "RelationHintV2",
    "SchemaMetadataV2",
    "TableSpecV2",
    "data_gen_spec_from_v2",
    "data_gen_spec_to_v2",
    "field_rule_from_v2",
    "field_rule_to_v2",
    "relation_hint_from_v2",
    "relation_hint_to_v2",
    "schema_metadata_from_v2",
    "schema_metadata_to_v2",
    "table_spec_from_v2",
    "table_spec_to_v2",
]


def _copy_list(lst: list[Any] | None) -> list[Any] | None:
    return list(lst) if lst is not None else None


class OverThresholdV2(VersionedModel):
    """OverThreshold 镜像（范围类阈值规则）。"""

    op: str = ">"
    value: float = 0.0
    share: float = 30.0


class FieldRuleV2(VersionedModel):
    """FieldRule 镜像：type/value/min/max/values/weights/table/column/hot/overThreshold/seq。"""

    type: str | None = None
    value: Any = None
    min: float | None = None
    max: float | None = None
    values: list[Any] | None = None
    weights: list[float] | None = None
    table: str | None = None
    column: str | None = None
    hot_percent: float | None = None
    hot_share: float | None = None
    over_threshold: OverThresholdV2 | None = None
    seq_prefix: str | None = None
    seq_start: int | None = None


class ColumnMetaV2(VersionedModel):
    """ColumnMeta 镜像（name 小写语义由旧模型构造时承接，映射不做二次变换）。"""

    name: str | None = None
    type_name: str | None = None
    size: int = 0
    nullable: bool = False
    auto_increment: bool = False
    primary_key: bool = False
    comment: str | None = None


class FkMetaV2(VersionedModel):
    """FkMeta 镜像。"""

    fk_column: str | None = None
    pk_table: str | None = None
    pk_column: str | None = None


class SchemaMetadataV2(VersionedModel):
    """SchemaMetadata 镜像（primary_keys 以 list 承载 set，往返还原）。"""

    table_name: str | None = None
    columns: list[ColumnMetaV2] = Field(default_factory=list)
    primary_keys: list[str] = Field(default_factory=list)
    foreign_keys: list[FkMetaV2] = Field(default_factory=list)
    row_count: int = -1


class RelationHintV2(VersionedModel):
    """RelationHint 镜像：四元组 + source/confidence/source_refs。"""

    child_table: str | None = None
    child_column: str | None = None
    parent_table: str | None = None
    parent_column: str | None = None
    source: str | None = None
    confidence: float = 0.0
    source_refs: list[str] = Field(default_factory=list)


class TableSpecV2(VersionedModel):
    """TableSpec 镜像。"""

    table_name: str | None = None
    row_count: int = 1000
    fields: dict[str, FieldRuleV2] = Field(default_factory=dict)
    orphan_rate: float | None = None


class DataGenSpecV2(VersionedModel):
    """DataGenSpec 镜像。

    - `max_rows`：legacy A1 钳制口径兼容承载，新管线不使用；
    - 无 `db_password`：凭据不进入模型（安全）。
    """

    jdbc_url: str | None = None
    db_user: str | None = None
    tables: list[TableSpecV2] = Field(default_factory=list)
    orphan_rate: float | None = None
    max_rows: int = 1_000_000  # legacy 钳制口径（A1 分轨）
    dry_run: bool = False


def field_rule_to_v2(rule: FieldRule) -> FieldRuleV2:
    over = rule.over_threshold
    return FieldRuleV2(
        type=rule.type,
        value=rule.value,
        min=rule.min,
        max=rule.max,
        values=_copy_list(rule.values),
        weights=_copy_list(rule.weights),
        table=rule.table,
        column=rule.column,
        hot_percent=rule.hot_percent,
        hot_share=rule.hot_share,
        over_threshold=OverThresholdV2(op=over.op, value=over.value, share=over.share)
        if over is not None
        else None,
        seq_prefix=rule.seq_prefix,
        seq_start=rule.seq_start,
    )


def field_rule_from_v2(v2: FieldRuleV2) -> FieldRule:
    over = v2.over_threshold
    return FieldRule(
        type=v2.type,
        value=v2.value,
        min=v2.min,
        max=v2.max,
        values=_copy_list(v2.values),
        weights=_copy_list(v2.weights),
        table=v2.table,
        column=v2.column,
        hot_percent=v2.hot_percent,
        hot_share=v2.hot_share,
        over_threshold=OverThreshold(op=over.op, value=over.value, share=over.share)
        if over is not None
        else None,
        seq_prefix=v2.seq_prefix,
        seq_start=v2.seq_start,
    )


def relation_hint_to_v2(hint: RelationHint) -> RelationHintV2:
    return RelationHintV2(
        child_table=hint.child_table,
        child_column=hint.child_column,
        parent_table=hint.parent_table,
        parent_column=hint.parent_column,
        source=hint.source,
        confidence=hint.confidence,
        source_refs=list(hint.source_refs),
    )


def relation_hint_from_v2(v2: RelationHintV2) -> RelationHint:
    hint = RelationHint(
        child_table=v2.child_table,
        child_column=v2.child_column,
        parent_table=v2.parent_table,
        parent_column=v2.parent_column,
        source=v2.source,
        confidence=v2.confidence,
    )
    hint.source_refs = list(v2.source_refs)
    return hint


def schema_metadata_to_v2(meta: SchemaMetadata) -> SchemaMetadataV2:
    return SchemaMetadataV2(
        table_name=meta.table_name,
        columns=[
            ColumnMetaV2(
                name=c.name,
                type_name=c.type_name,
                size=c.size,
                nullable=c.nullable,
                auto_increment=c.auto_increment,
                primary_key=c.primary_key,
                comment=c.comment,
            )
            for c in meta.columns
        ],
        primary_keys=sorted(meta.primary_keys),
        foreign_keys=[
            FkMetaV2(fk_column=f.fk_column, pk_table=f.pk_table, pk_column=f.pk_column)
            for f in meta.foreign_keys
        ],
        row_count=meta.row_count,
    )


def schema_metadata_from_v2(v2: SchemaMetadataV2) -> SchemaMetadata:
    meta = SchemaMetadata(
        table_name=v2.table_name,
        columns=[
            ColumnMeta(
                name=c.name,
                type_name=c.type_name,
                size=c.size,
                nullable=c.nullable,
                auto_increment=c.auto_increment,
                primary_key=c.primary_key,
                comment=c.comment,
            )
            for c in v2.columns
        ],
        foreign_keys=[
            FkMeta(fk_column=f.fk_column, pk_table=f.pk_table, pk_column=f.pk_column)
            for f in v2.foreign_keys
        ],
        row_count=v2.row_count,
    )
    meta.primary_keys = set(v2.primary_keys)
    return meta


def table_spec_to_v2(spec: TableSpec) -> TableSpecV2:
    return TableSpecV2(
        table_name=spec.table_name,
        row_count=spec.row_count,
        fields={k: field_rule_to_v2(v) for k, v in spec.fields.items()},
        orphan_rate=spec.orphan_rate,
    )


def table_spec_from_v2(v2: TableSpecV2) -> TableSpec:
    return TableSpec(
        table_name=v2.table_name,
        row_count=v2.row_count,
        fields={k: field_rule_from_v2(v) for k, v in v2.fields.items()},
        orphan_rate=v2.orphan_rate,
    )


def data_gen_spec_to_v2(spec: DataGenSpec) -> DataGenSpecV2:
    """旧规格 -> V2 镜像。db_password 不迁移（凭据不进模型）。"""
    return DataGenSpecV2(
        jdbc_url=spec.jdbc_url,
        db_user=spec.db_user,
        tables=[table_spec_to_v2(t) for t in spec.tables],
        orphan_rate=spec.orphan_rate,
        max_rows=spec.max_rows,
        dry_run=spec.dry_run,
    )


def data_gen_spec_from_v2(v2: DataGenSpecV2, db_password: str | None = None) -> DataGenSpec:
    """V2 镜像 -> 旧规格。db_password 由调用方从连接层取回。"""
    return DataGenSpec(
        jdbc_url=v2.jdbc_url,
        db_user=v2.db_user,
        db_password=db_password,
        tables=[table_spec_from_v2(t) for t in v2.tables],
        orphan_rate=v2.orphan_rate,
        max_rows=v2.max_rows,
        dry_run=v2.dry_run,
    )
