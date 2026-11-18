"""PTDE 项目分析引擎（R1，批次 B1）。

收编 `datagen/` Java 栈解析器为第一个 Language Adapter；发现统一
CONFIRMED/INFERRED/UNKNOWN 三态；A3 分级清单与 ptde_finding 采纳流落地。

对外入口：
- `JavaAdapter`：Java 栈分析（MyBatis/JPA + 关联四级合并 + 访问模式 + 枚举 + 布局识别）
- `RelationResolver` / `NamingRelationResolver`：关联三级三态解析
- `AccessPatternExtractor`：SQL → AccessPattern
- `EnumFinder`：枚举四级发现（分级证据）
- `FindingPolicy`（enforce/evaluate）：A3 分级裁决
- `FindingRepository`：ptde_finding 落库与采纳流
- `SnapshotBuilder`：ProjectSnapshot 产出
"""

from ptde.analyzer.access_patterns import AccessPatternExtractor
from ptde.analyzer.enum_finder import EnumFinder
from ptde.analyzer.finding_policy import (
    HardConstraintViolation,
    PolicyResult,
    classify,
    enforce,
    evaluate,
)
from ptde.analyzer.finding_repo import FindingRepository, PtdeFindingModel
from ptde.analyzer.lang_java.adapter import JavaAdapter  # noqa: E402  延后于基础模块导入（避免环）
from ptde.analyzer.language_adapter import (
    AnalyzerOutput,
    Finding,
    FindingStatus,
    LanguageAdapter,
)
from ptde.analyzer.naming_resolver import NamingRelationResolver
from ptde.analyzer.relation_resolver import RelationResolution, RelationResolver
from ptde.analyzer.snapshot_builder import SnapshotBuilder

__all__ = [
    "AccessPatternExtractor",
    "AnalyzerOutput",
    "EnumFinder",
    "Finding",
    "FindingRepository",
    "FindingStatus",
    "HardConstraintViolation",
    "JavaAdapter",
    "LanguageAdapter",
    "NamingRelationResolver",
    "PolicyResult",
    "PtdeFindingModel",
    "RelationResolution",
    "RelationResolver",
    "SnapshotBuilder",
    "classify",
    "enforce",
    "evaluate",
]
