"""数据模型：移植自 model/*.java（FieldRule/SchemaMetadata/RelationHint/DataGenSpec/TableSpec）。

对齐点：
- 表名/列名构造时统一小写（规避 MySQL 大小写不敏感与 PG/Oracle 大小写敏感差异）；
- RelationHint 构造时四元组小写；priority/autoApplicable 语义不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OverThreshold:
    """范围类阈值规则：保证 share% 的行取 (value, max] 区间（op 为 > 或 >=）。"""

    op: str = ">"
    value: float = 0.0
    share: float = 30.0


@dataclass
class FieldRule:
    """字段生成规则（规则 JSON 契约的强类型模型）。

    type: fixed/range/enum/ref/refSkew/seq（未知类型回落默认生成器）。
    seq 值 = seq_prefix + (seq_start + row_index)；seq_start 由宿主执行时刷新。
    """

    TYPE_FIXED = "fixed"
    TYPE_RANGE = "range"
    TYPE_ENUM = "enum"
    TYPE_REF = "ref"
    TYPE_REF_SKEW = "refSkew"
    TYPE_SEQ = "seq"

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
    over_threshold: OverThreshold | None = None
    seq_prefix: str | None = None
    seq_start: int | None = None

    @classmethod
    def fixed(cls, value: Any) -> FieldRule:
        r = cls()
        r.type = cls.TYPE_FIXED
        r.value = value
        return r


@dataclass
class ColumnMeta:
    """列元信息；name 构造时小写。"""

    name: str | None = None
    type_name: str | None = None
    size: int = 0
    nullable: bool = False
    auto_increment: bool = False
    primary_key: bool = False
    comment: str | None = None

    def __post_init__(self) -> None:
        if self.name is not None:
            self.name = self.name.lower()


@dataclass
class FkMeta:
    """外键（声明式约束；无外键库由 RelationResolver 语义推断补齐）。"""

    fk_column: str | None = None
    pk_table: str | None = None
    pk_column: str | None = None

    def __post_init__(self) -> None:
        if self.fk_column is not None:
            self.fk_column = self.fk_column.lower()
        if self.pk_table is not None:
            self.pk_table = self.pk_table.lower()
        if self.pk_column is not None:
            self.pk_column = self.pk_column.lower()


@dataclass
class SchemaMetadata:
    """统一 schema 元数据（由 DatabaseDialect.fetchMetadata 产出）。

    row_count 为当前行数（空表兜底依据；方言无法快速获取时 -1）。
    """

    table_name: str | None = None
    columns: list[ColumnMeta] = field(default_factory=list)
    primary_keys: set[str] = field(default_factory=set)
    foreign_keys: list[FkMeta] = field(default_factory=list)
    row_count: int = -1

    def __post_init__(self) -> None:
        if self.table_name is not None:
            self.table_name = self.table_name.lower()

    def find_column(self, name: str | None) -> ColumnMeta | None:
        """列名查找（大小写不敏感）。"""
        if name is None:
            return None
        for c in self.columns:
            if c.name is not None and c.name.lower() == name.lower():
                return c
        return None

    def is_null_or_empty_table(self) -> bool:
        return self.row_count <= 0

    def column_index(self) -> dict[str, ColumnMeta]:
        """按列名索引（内部使用）。"""
        return {c.name: c for c in self.columns if c.name is not None}


@dataclass
class RelationHint:
    """表间关联提示：RelationResolver 四级来源的统一载体。

    来源优先级（高 → 低）：explicit > db_fk > orm_join > naming / naming_empty_fallback。
    同一条边多来源时取最高优先级；naming 类低置信推断不静默生效。
    """

    SRC_EXPLICIT = "explicit"
    SRC_DB_FK = "db_fk"
    SRC_ORM_JOIN = "orm_join"
    SRC_NAMING = "naming"
    SRC_NAMING_EMPTY = "naming_empty_fallback"

    child_table: str | None = None
    child_column: str | None = None
    parent_table: str | None = None
    parent_column: str | None = None
    source: str | None = None
    confidence: float = 0.0
    source_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for f in ("child_table", "child_column", "parent_table", "parent_column"):
            v = getattr(self, f)
            if v is not None:
                setattr(self, f, v.lower())

    @classmethod
    def naming_fallback(
        cls, child_table: str, child_column: str, parent_table: str, parent_column: str
    ) -> RelationHint:
        """空表兜底工厂：命名推断无条件采纳但低置信（0.3）。"""
        h = cls(child_table, child_column, parent_table, parent_column,
                cls.SRC_NAMING_EMPTY, 0.3)
        h.source_refs.append("naming-convention(empty-table-fallback)")
        return h

    def priority(self) -> int:
        """来源优先级数值（越大越优先）。"""
        return {
            self.SRC_EXPLICIT: 40,
            self.SRC_DB_FK: 30,
            self.SRC_ORM_JOIN: 20,
            self.SRC_NAMING: 10,
            self.SRC_NAMING_EMPTY: 10,
        }.get(self.source or "", 0)

    def auto_applicable(self) -> bool:
        """db_fk / orm_join / explicit 高置信自动生效；naming 系需用户确认。"""
        return self.source in (self.SRC_EXPLICIT, self.SRC_DB_FK, self.SRC_ORM_JOIN)


@dataclass
class SourceFile:
    """可选源码文件（.java/.xml，MyBatis 解析与 Java 枚举反查输入）。"""

    path: str | None = None
    content: str | None = None


@dataclass
class TableSpec:
    """表组元素：联表灌数的单表配置。

    fields 的键为目标列名（大小写不敏感，引擎侧统一小写处理）。
    """

    table_name: str | None = None
    row_count: int = 1000
    fields: dict[str, FieldRule] = field(default_factory=dict)
    orphan_rate: float | None = None


@dataclass
class DataGenSpec:
    """灌数规格：宿主 → 引擎的唯一入参模型。

    source_files 为可选源码输入，为空时引擎降级为纯数据库元数据模式。
    """

    jdbc_url: str | None = None
    db_user: str | None = None
    db_password: str | None = None
    tables: list[TableSpec] = field(default_factory=list)
    orphan_rate: float | None = None
    max_rows: int = 1_000_000
    dry_run: bool = False
    source_files: list[SourceFile] | None = None
