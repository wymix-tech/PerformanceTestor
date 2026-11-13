"""数据模型：移植自 model/*.java（FieldRule/SchemaMetadata/RelationHint/DataGenSpec/TableSpec）。

对齐点：
- 表名/列名构造时统一小写（规避 MySQL 大小写不敏感与 PG/Oracle 大小写敏感差异）；
- RelationHint 构造时四元组小写；priority/autoApplicable 语义不变。
"""

from __future__ import annotations

import enum
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
class MagicHint:
    """魔法值提示：源码扫描产出的「表列 → 候选值」建议（含来源定位与范围谓词阈值）。

    宿主落 semantic_hint 库，经建议接口返回前端一键采纳为生成规则。
    """

    KIND_LITERAL = "literal"    # SQL 字面量（WHERE status = 'PAID'）
    KIND_CONSTANT = "constant"  # static final 常量
    KIND_ENUM = "enum"          # Java 枚举常量
    KIND_RANGE = "range"        # 比较谓词（amount > 1000）

    table: str | None = None
    column: str | None = None
    kind: str | None = None
    values: list[Any] = field(default_factory=list)
    threshold: OverThreshold | None = None
    source_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.table is not None:
            self.table = self.table.lower()
        if self.column is not None:
            self.column = self.column.lower()


class SqlAccess(enum.Enum):
    """SQL 访问类型：读（SELECT）/ 写（INSERT/UPDATE/DELETE）。"""

    READ = "READ"
    WRITE = "WRITE"


class Op(enum.Enum):
    """谓词算子。"""

    EQ = "EQ"        # 等值：col = v
    IN = "IN"        # 集合：col IN (v1, v2)
    RANGE = "RANGE"  # 范围：col > v / col < v / col BETWEEN a AND b


@dataclass
class Predicate:
    """SQL 谓词：列 + 算子 + 取值（EQ/IN 用 values；RANGE 用 min/max）。"""

    column: str | None = None
    op: Op | None = None
    values: list[Any] = field(default_factory=list)
    min: float | None = None
    max: float | None = None

    def flat_values(self) -> list[Any]:
        """谓词涉及的全部取值（RANGE 为空列表）。"""
        return self.values

    def __str__(self) -> str:
        if self.op == Op.RANGE:
            return f"{self.column} {self.op} [{self.min},{self.max}]"
        return f"{self.column} {self.op} {self.values}"


@dataclass
class JoinRef:
    """外键引用：本表外键列 → 父表列。"""

    child_column: str
    parent_table: str
    parent_column: str
    confidence: float

    def __str__(self) -> str:
        return f"{self.child_column} → {self.parent_table}.{self.parent_column}"


@dataclass
class EndpointTableFact:
    """接口访问事实：单个压测接口在一张表上的访问结论（供宿主编排层推导行数与生成规则）。

    未解析出结论时 unresolved_reason 非空，由前端黄色提示引导手工补表，不做静默猜测。
    """

    table: str | None = None
    access: SqlAccess = SqlAccess.READ
    predicates: list[Predicate] = field(default_factory=list)
    join_tables: list[str] = field(default_factory=list)
    join_refs: list[JoinRef] = field(default_factory=list)
    # 跨 SQL 累积的被引用列名（N+1 形态下外键列可能只在单条 SQL 里出现）
    columns: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    confidence: float = 1.0
    # 非空表示降级原因（未覆盖/子查询/多表 UPDATE/超时等）
    unresolved_reason: str | None = None
    # 分页大小（SQL limit 字面量，0 表示未识别）
    page_size: int = 0
    # 是否为详情类访问（按 id 查单行，数据应做热点倾斜）
    detail_lookup: bool = False

    def __post_init__(self) -> None:
        if self.table is not None:
            self.table = self.table.lower()

    def __str__(self) -> str:
        return f"{self.table}/{self.access} predicates={self.predicates} conf={self.confidence}"


@dataclass
class EndpointRef:
    """HTTP 接口定位：AST 解析 Controller 产出的方法级坐标。

    className 是 simpleName（文件名去 .java），不是 FQCN；字段语义必须与宿主
    正则实现的产出完全一致，否则按 (httpMethod, path) join 时会失配。
    """

    http_method: str | None = None
    path: str | None = None
    class_name: str | None = None
    method_name: str | None = None
    declaring_file: str | None = None
    line: int = 0

    def key(self) -> str:
        """匹配键：与方法无关的精确匹配键（HTTP 方法 + 路径）。"""
        return ((self.http_method or "").upper() + " " + (self.path or ""))

    def path_key(self) -> str:
        """兜底匹配键：忽略 HTTP 方法。"""
        return self.path or ""

    def member_key(self) -> str:
        """兜底匹配键：文件 + 方法名。"""
        return (self.declaring_file or "") + "#" + (self.method_name or "")

    def class_member_key(self) -> str:
        """兜底匹配键：类名（simpleName）+ 方法名。"""
        return (self.class_name or "") + "#" + (self.method_name or "")

    def __str__(self) -> str:
        return f"{self.key()} ({self.member_key()}:{self.line})"


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
