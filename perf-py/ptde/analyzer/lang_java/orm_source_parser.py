"""ORM 源码解析 SPI：移植自 spi/OrmSourceParser.java。

从业务源码中提取魔法值候选与表间关联语义。实现必须容错：单文件解析失败/超时
静默跳过，绝不因 SQL 方言碎片化中断整个灌数流程。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ptde.analyzer.lang_java._legacy_model import MagicHint, RelationHint, SourceFile


@dataclass
class ScanResult:
    """扫描产物：魔法值候选 + 关联语义 + Java 枚举候选（枚举反查第四级来源）。"""

    magic_hints: list[MagicHint] = field(default_factory=list)
    relation_hints: list[RelationHint] = field(default_factory=list)
    java_enums: dict[str, list[str]] = field(default_factory=dict)


class OrmSourceParser(ABC):

    @abstractmethod
    def supports(self, files: list[SourceFile] | None) -> bool:
        """特征检测：源码集合中是否出现该 ORM 的特征。"""

    @abstractmethod
    def order(self) -> int:
        """多 ORM 共存时的解析优先级（小者优先）。"""

    @abstractmethod
    def scan(self, files: list[SourceFile], known_tables) -> ScanResult:
        """扫描源码产出语义提示（单文件失败/超时静默跳过）。"""
