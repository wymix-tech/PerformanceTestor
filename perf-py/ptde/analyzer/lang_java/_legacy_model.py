"""Java 解析器 / 分析链模型居所（兼容 re-export 垫片）。

演进史：
- T1.2～T7.9 期间：本模块按原样复制 ``datagen.model`` 的解析器子集，
  与灌数宿主链的 ``ptde.models.wire`` 双居所并存（彼时受 A4 反向依赖
  禁令约束——datagen 包尚存，ptde 不得反向 import）。
- T7.9 退役后技术债清理（2026-11-20）：datagen 包已删除，双居所成因
  消失。重复类（OverThreshold/RelationHint/SourceFile/MagicHint/
  SqlAccess/Op/Predicate/JoinRef/EndpointRef）统一以
  ``ptde.models.wire`` 为唯一居所（语义逐类核对等价：构造小写、
  priority/auto_applicable、枚举值一一相同；wire 版 JoinRef/EndpointRef
  多 ``__str__`` 为纯增量），本模块退化为分析链导入路径的兼容垫片。

分析链（lang_java 解析器 / workload.compose / llm_enhance /
datagen_plan_service）继续从本模块导入即可；新代码建议直接从
``ptde.models.wire`` 导入。

兼容用途：保留分析链既有导入路径（枚举身份比较依赖同一模型对象），
避免一次性大范围改路；预计下线条件：上述调用方及相关契约测试全部改路
至 ``ptde.models.wire``、全局检索无残留导入后，删除本垫片。
"""

from __future__ import annotations

from ptde.models.wire import (  # noqa: F401  兼容 re-export（单一居所）
    EndpointRef,
    JoinRef,
    MagicHint,
    Op,
    OverThreshold,
    Predicate,
    RelationHint,
    SourceFile,
    SqlAccess,
)

__all__ = [
    "EndpointRef",
    "JoinRef",
    "MagicHint",
    "Op",
    "OverThreshold",
    "Predicate",
    "RelationHint",
    "SqlAccess",
    "SourceFile",
]
