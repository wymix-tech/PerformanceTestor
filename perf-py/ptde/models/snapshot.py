"""快照模型：ProjectSnapshot / DatabaseSchemaSnapshot（总方案 §5.1、实施方案 §3.4）。

R0 先落结构；R1 补采集实现（现状方言仅采集列/主键/声明外键/行数，见 F2）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from ptde.models.base import DbRef, Evidence, VersionedModel

__all__ = [
    "CheckConstraintSchema",
    "ColumnSchema",
    "DatabaseSchemaSnapshot",
    "DependencyRef",
    "ForeignKeySchema",
    "GeneratedColumnSchema",
    "IndexColumn",
    "IndexSchema",
    "PartitionSchema",
    "ProjectSnapshot",
    "SequenceSchema",
    "TableSchema",
    "TechStackEntry",
    "TriggerSchema",
    "UniqueConstraintSchema",
]


class TechStackEntry(VersionedModel):
    """技术栈条目：声明依赖与运行时证据分离（总方案 §3.2）。

    declared 来自构建文件（pom.xml/build.gradle）；runtime 来自运行时连接/探测。
    无法确认时 status=UNKNOWN（不得伪装）。
    """

    declared: str | None = None
    runtime: str | None = None
    status: str = "UNKNOWN"  # CONFIRMED / INFERRED / UNKNOWN


class DependencyRef(VersionedModel):
    """项目依赖声明。"""

    name: str
    version: str | None = None
    manager: str | None = None  # maven / gradle / npm ...
    scope: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class ProjectSnapshot(VersionedModel):
    """项目快照：源码版本 + 技术栈 + 依赖 + 配置指纹。"""

    exclude_fingerprint_fields = frozenset({"analyzed_at"})

    id: str
    source_revision: str  # git commit / zip 内容哈希
    tech_stack: dict[str, TechStackEntry] = Field(default_factory=dict)
    dependencies: list[DependencyRef] = Field(default_factory=list)
    schema_fingerprint: str | None = None
    config_fingerprint: str | None = None
    analyzed_at: datetime | None = None
    analyzer_version: str = "0.0.0"
    evidence: list[Evidence] = Field(default_factory=list)


class IndexColumn(VersionedModel):
    """索引列：含复合索引列序（SEQ_IN_INDEX）。"""

    name: str
    ordinal: int = 1  # 列序，1 起
    direction: str | None = None  # ASC / DESC


class IndexSchema(VersionedModel):
    """索引（含列序；R1 由 information_schema.STATISTICS / pg_index 采集）。"""

    name: str
    columns: list[IndexColumn] = Field(default_factory=list)
    unique: bool = False
    method: str | None = None  # btree / hash / gin ...
    evidence: list[Evidence] = Field(default_factory=list)


class ColumnSchema(VersionedModel):
    """列结构。"""

    name: str
    type_name: str
    nullable: bool = True
    primary_key: bool = False
    auto_increment: bool = False
    default: str | None = None
    comment: str | None = None
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    ordinal: int = 0
    evidence: list[Evidence] = Field(default_factory=list)


class ForeignKeySchema(VersionedModel):
    """声明式外键。"""

    name: str | None = None
    child_columns: list[str] = Field(default_factory=list)
    parent_table: str
    parent_columns: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class UniqueConstraintSchema(VersionedModel):
    """唯一约束（R1 采集；现状仅主键隐含唯一）。"""

    name: str
    columns: list[str] = Field(default_factory=list)


class CheckConstraintSchema(VersionedModel):
    """CHECK 约束（MySQL 8.0.16+ / PG）。"""

    name: str | None = None
    expression: str
    columns: list[str] = Field(default_factory=list)


class PartitionSchema(VersionedModel):
    """分区信息（R1 由 INFORMATION_SCHEMA.PARTITIONS / pg_inherits 采集）。"""

    name: str
    method: str | None = None  # RANGE / LIST / HASH ...
    expression: str | None = None
    rows: int | None = None


class TriggerSchema(VersionedModel):
    """触发器（R1 采集；装载前副作用检查输入，总方案 §9.4）。"""

    name: str
    timing: str | None = None  # BEFORE / AFTER
    event: str | None = None  # INSERT / UPDATE / DELETE
    statement: str | None = None


class GeneratedColumnSchema(VersionedModel):
    """生成列（R1 采集：GENERATION_EXPRESSION / pg_attribute.attgenerated）。"""

    name: str
    expression: str
    kind: str | None = None  # STORED / VIRTUAL


class SequenceSchema(VersionedModel):
    """Sequence（R1 采集：pg_sequences；MySQL 以 AUTO_INCREMENT 属性显式登记）。"""

    name: str
    current_value: int | None = None
    increment: int | None = None


class TableSchema(VersionedModel):
    """单表结构快照：全要素结构（R0 落结构，R1 补采集实现）。"""

    name: str
    columns: list[ColumnSchema] = Field(default_factory=list)
    indexes: list[IndexSchema] = Field(default_factory=list)  # 含复合索引列序
    primary_keys: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeySchema] = Field(default_factory=list)
    unique_constraints: list[UniqueConstraintSchema] = Field(default_factory=list)
    check_constraints: list[CheckConstraintSchema] = Field(default_factory=list)
    partitions: list[PartitionSchema] = Field(default_factory=list)
    triggers: list[TriggerSchema] = Field(default_factory=list)
    generated_columns: list[GeneratedColumnSchema] = Field(default_factory=list)
    sequences: list[SequenceSchema] = Field(default_factory=list)
    row_estimate: int = -1  # -1 = 方言无法获取（对齐 legacy SchemaMetadata.row_count 语义）
    table_comment: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class DatabaseSchemaSnapshot(VersionedModel):
    """目标数据库结构快照：任一字段必须可溯源到方言查询或 DDL（A3）。"""

    exclude_fingerprint_fields = frozenset({"collected_at"})

    id: str
    database: DbRef | None = None
    tables: list[TableSchema] = Field(default_factory=list)
    collected_at: datetime | None = None
    collector_version: str = "0.0.0"
