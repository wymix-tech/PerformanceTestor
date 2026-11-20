"""Java 解析器 / 分析链模型居所（自 datagen.model 原样收编子集，T7.9 退役后保留）。

T1.2 收编的 Java 解析器与 T1.3 关联解析器依赖 datagen.model 的 dataclass 语义
（构造时小写、priority/auto_applicable 等）。为避免 ptde 反向 import datagen
（A4 反向依赖禁令），此处按原样复制所需子集；行为与 legacy 完全一致，
两套并存期间以单测对照保证同输入同输出（实施方案 §4.6）。

B7 续批次 11（T7.9 退役）：datagen 包已删除，本模块成为分析链
（lang_java 解析器 / workload.compose / llm_enhance / datagen_plan_service）
的模型唯一居所——与灌数宿主链的 ``ptde.models.wire`` 并存不合并
（本子集含三态字段等价演进，枚举跨模块不可比）。

来源：perf-py/datagen/model.py（仅复制 9 个解析器 + 关联解析所需类）。
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
class RelationHint:
    """表间关联提示：RelationResolver 四级来源的统一载体（副本）。"""

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
        h = cls(child_table, child_column, parent_table, parent_column,
                cls.SRC_NAMING_EMPTY, 0.3)
        h.source_refs.append("naming-convention(empty-table-fallback)")
        return h

    def priority(self) -> int:
        return {
            self.SRC_EXPLICIT: 40,
            self.SRC_DB_FK: 30,
            self.SRC_ORM_JOIN: 20,
            self.SRC_NAMING: 10,
            self.SRC_NAMING_EMPTY: 10,
        }.get(self.source or "", 0)

    def auto_applicable(self) -> bool:
        return self.source in (self.SRC_EXPLICIT, self.SRC_DB_FK, self.SRC_ORM_JOIN)


@dataclass
class SourceFile:
    """可选源码文件（.java/.xml，MyBatis 解析与 Java 枚举反查输入）。"""

    path: str | None = None
    content: str | None = None


@dataclass
class MagicHint:
    """魔法值提示：源码扫描产出的「表列 → 候选值」建议（副本）。"""

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
    """谓词算子（副本）。"""

    EQ = "EQ"        # 等值：col = v
    IN = "IN"        # 集合：col IN (v1, v2)
    RANGE = "RANGE"  # 范围：col > v / col < v / col BETWEEN a AND b


@dataclass
class Predicate:
    """SQL 谓词：列 + 算子 + 取值（副本；__str__ 与 legacy 一致）。"""

    column: str | None = None
    op: Op | None = None
    values: list[Any] = field(default_factory=list)
    min: float | None = None
    max: float | None = None

    def flat_values(self) -> list[Any]:
        return self.values

    def __str__(self) -> str:
        if self.op == Op.RANGE:
            return f"{self.column} {self.op} [{self.min},{self.max}]"
        return f"{self.column} {self.op} {self.values}"


@dataclass
class JoinRef:
    """外键引用：本表外键列 → 父表列（副本）。"""

    child_column: str
    parent_table: str
    parent_column: str
    confidence: float


@dataclass
class EndpointRef:
    """HTTP 接口定位：AST 解析 Controller 产出的方法级坐标（副本）。"""

    http_method: str | None = None
    path: str | None = None
    class_name: str | None = None
    method_name: str | None = None
    declaring_file: str | None = None
    line: int = 0

    def key(self) -> str:
        return ((self.http_method or "").upper() + " " + (self.path or ""))

    def path_key(self) -> str:
        return self.path or ""

    def member_key(self) -> str:
        return (self.declaring_file or "") + "#" + (self.method_name or "")

    def class_member_key(self) -> str:
        return (self.class_name or "") + "#" + (self.method_name or "")
