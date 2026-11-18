"""实体与关系模型：EntityModel / RelationModel / BusinessInvariant（总方案 §5.1～§5.3）。

RelationModel 承接 legacy RelationHint 四元组 + source/confidence/source_refs 语义
（R1 由 relation_resolver/naming_resolver 收编，输出补 status/evidence 三态）。
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import Field

from ptde.models.base import Evidence, EvidenceStatus, VersionedModel

__all__ = [
    "BusinessInvariant",
    "Cardinality",
    "EntityModel",
    "FieldModel",
    "InvariantPredicate",
    "RelationKind",
    "RelationModel",
    "RelationRef",
]


class Cardinality(enum.StrEnum):
    """关系基数。"""

    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class RelationKind(enum.StrEnum):
    """关系来源类别：声明外键 / ORM 关系 / 逻辑关联（命名与语义推断）。"""

    DB_FK = "db_fk"
    ORM = "orm"
    LOGICAL = "logical"


class RelationRef(VersionedModel):
    """实体内关系引用（EntityModel.relations 元素，对齐 §5.2 示例）。"""

    target: str  # 目标实体 id
    cardinality: Cardinality = Cardinality.MANY_TO_ONE
    join_fields: list[str] = Field(default_factory=list)


class FieldModel(VersionedModel):
    """实体字段（对齐总方案 §5.2 示例）。"""

    name: str
    type: str  # bigint / varchar / decimal ...
    nullable: bool = True
    primary_key: bool = False
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    allowed_values: list[Any] = Field(default_factory=list)
    generation_strategy: str | None = None  # planned_range / distribution / ...
    business_rule_ref: str | None = None  # 关联 BusinessInvariant.id


class EntityModel(VersionedModel):
    """业务实体：字段 + 关系 + 证据。"""

    id: str
    table: str
    fields: list[FieldModel] = Field(default_factory=list)
    relations: list[RelationRef] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class RelationModel(VersionedModel):
    """表间关系：四元组 + 三态状态 + 证据（A3：naming/注解 SQL 一律 INFERRED）。"""

    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    cardinality: Cardinality = Cardinality.MANY_TO_ONE
    relation_kind: RelationKind = RelationKind.LOGICAL
    status: EvidenceStatus = EvidenceStatus.UNKNOWN
    confidence: float = 0.0
    evidence: list[Evidence] = Field(default_factory=list)


class InvariantPredicate(VersionedModel):
    """业务不变量结构化谓词（对齐总方案 §5.3 relation_exists 形态）。

    无法结构化表达的规则登记为 status=UNKNOWN 的待人工审核验证器，
    不自动执行（R6 invariant_validator 仅接受 CONFIRMED）。
    """

    type: str  # relation_exists / ...
    when: dict[str, Any] | None = None
    require: dict[str, Any] | None = None
    note: str | None = None


class BusinessInvariant(VersionedModel):
    """跨字段、跨实体业务不变量。"""

    id: str
    description: str
    status: EvidenceStatus = EvidenceStatus.UNKNOWN
    scope_entities: list[str] = Field(default_factory=list)
    predicate: InvariantPredicate | None = None
    evidence: list[Evidence] = Field(default_factory=list)
