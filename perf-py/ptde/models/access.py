"""访问模式模型：AccessPattern（承接 legacy SqlAccess/Predicate/JoinRef 语义）。

R1 由 sql_table_extractor/sql_predicate_extractor/sql_literal_extractor 收编产出。
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import Field

from ptde.models.base import VersionedModel

__all__ = ["AccessPattern", "JoinRef", "LimitOffset", "Predicate", "PredicateOp", "SqlKind"]


class SqlKind(enum.StrEnum):
    """SQL 访问类型：读 / 写（对齐 legacy SqlAccess.READ/WRITE）。"""

    READ = "READ"
    WRITE = "WRITE"


class PredicateOp(enum.StrEnum):
    """谓词算子（对齐 legacy Op.EQ/IN/RANGE）。"""

    EQ = "EQ"
    IN = "IN"
    RANGE = "RANGE"


class Predicate(VersionedModel):
    """SQL 谓词：列 + 算子 + 取值（EQ/IN 用 values；RANGE 用 min/max）。"""

    column: str
    op: PredicateOp = PredicateOp.EQ
    values: list[Any] = Field(default_factory=list)
    min: float | None = None
    max: float | None = None


class JoinRef(VersionedModel):
    """外键引用：本表外键列 → 父表列。"""

    child_column: str
    parent_table: str
    parent_column: str
    confidence: float = 1.0


class LimitOffset(VersionedModel):
    """分页要素（SQL limit 字面量；0 表示未识别）。"""

    limit: int = 0
    offset: int = 0


class AccessPattern(VersionedModel):
    """结构化访问模式：表 / JOIN / WHERE / ORDER BY / 选择性线索。"""

    id: str
    sql_kind: SqlKind = SqlKind.READ
    tables: list[str] = Field(default_factory=list)
    joins: list[JoinRef] = Field(default_factory=list)
    filters: list[Predicate] = Field(default_factory=list)
    order_by: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    limit_offset: LimitOffset | None = None
    range_fields: list[str] = Field(default_factory=list)
    time_window: dict[str, str] | None = None  # {"from": ..., "to": ...}
    selectivity_hints: dict[str, float] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
    # 来源 SQL 无法解析时置 True（UNKNOWN 登记而非丢弃，A3）
    unresolved: bool = False
    unresolved_reason: str | None = None
