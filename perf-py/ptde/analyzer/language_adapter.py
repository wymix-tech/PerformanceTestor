"""Language Adapter 接口定义（T1.1）。

按多语言预留（本期仅实现 Java，见 `ptde/analyzer/lang_java/`）。接口约束：

1. `analyze(paths) -> AnalyzerOutput`：输出为结构化模型（ptde.models +
   Finding），禁止文本/自然语言作为模块间传递依据（总方案 §2.4 模型先行）。
2. 每条发现统一三态 `CONFIRMED/INFERRED/UNKNOWN`（总方案 §3.6），证据为
   结构化引用（文件路径+行号、SQL/DB 对象名），不接受纯文本断言。
3. 未知项显式登记（UNKNOWN），不得静默丢弃（A3 分级显式）。
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import Field

from ptde.models.access import AccessPattern
from ptde.models.base import Evidence, EvidenceStatus, VersionedModel
from ptde.models.entity import EntityModel, RelationModel

__all__ = [
    "AnalyzerOutput",
    "Finding",
    "FindingStatus",
    "LanguageAdapter",
]


class FindingStatus(enum.StrEnum):
    """发现三态（总方案 §3.6；与 EvidenceStatus 同语义，独立枚举便于落库）。"""

    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class Finding(VersionedModel):
    """分析发现（总方案 §3.6 最小字段集）。

    对齐 `ptde_finding` 表（实施方案 §12.2）：value 为结构化内容（落库时
    规范 JSON 化并计算 value_fingerprint 作为幂等 upsert 键）。
    """

    id: str
    type: str  # relation / access_pattern / enum / business_rule / tech_stack / migration_file / test_fixture / ...
    value: dict[str, Any] = Field(default_factory=dict)
    source_file: str | None = None
    database_object: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = 0.0
    status: FindingStatus = FindingStatus.UNKNOWN
    analyzer_version: str = "0.0.0"

    def to_evidence_status(self) -> EvidenceStatus:
        """映射到模型层三态枚举（RelationModel.status 等）。"""
        return EvidenceStatus(self.status.value)


class AnalyzerOutput(VersionedModel):
    """Language Adapter 分析输出：全结构化，无自然语言执行依据。"""

    id: str
    analyzer_version: str = "0.0.0"
    source_revision: str | None = None
    entities: list[EntityModel] = Field(default_factory=list)
    relations: list[RelationModel] = Field(default_factory=list)
    access_patterns: list[AccessPattern] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class LanguageAdapter(ABC):
    """语言适配器抽象基类（多语言预留，本期实现 JavaAdapter）。"""

    #: 适配器标识（如 "java"），进 AnalyzerOutput.analyzer_version 记录
    name: str = "base"

    @abstractmethod
    def supports(self, project_root: Path) -> bool:
        """按项目结构判断是否适用（如存在构建文件/源码目录），不得仅凭目录名猜测。"""

    @abstractmethod
    def analyze(self, paths: list[Path]) -> AnalyzerOutput:
        """分析给定源码路径集合，输出结构化 AnalyzerOutput。"""
