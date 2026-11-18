"""A3 分级清单（T1.7，后续阶段共用）。

分级（实施方案 §4.4）：

| 类别 | 项 | 缺失/失败时行为 |
|---|---|---|
| 硬约束类 | 主键缺失、声明外键解析失败、唯一约束、方言元数据残缺、Schema 快照失败 | 显式失败（严格与非严格模式均阻断），错误带对象名与原因 |
| 推断类 | naming 关联、注解 SQL、枚举猜测、Java 枚举反查、MagicHint、迁移/测试文件识别 | warning + 登记 ptde_finding，不阻断 |
| 可配置 | strict_mode=True 时推断类也阻断（ptde.config） |

错误信息必须带对象名与原因（不得只给类别名）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "HARD_TYPES",
    "INFERRED_TYPES",
    "HardConstraintViolation",
    "PolicyResult",
    "classify",
    "evaluate",
    "enforce",
]

# 硬约束类发现类型（缺失/失败 → 显式失败）
HARD_TYPES: frozenset[str] = frozenset({
    "primary_key_missing",
    "fk_parse_failed",
    "unique_constraint_missing",
    "metadata_incomplete",
    "schema_snapshot_failed",
})

# 推断类发现类型（warning + 采纳流，不阻断）
INFERRED_TYPES: frozenset[str] = frozenset({
    "naming_relation",
    "annotation_sql_join",
    "orm_join",
    "enum_guess",
    "column_comment",
    "java_enum_reflect",
    "magic_hint",
    "tech_stack_guess",
    "migration_file",
    "test_fixture",
})


def classify(finding: dict[str, Any]) -> str:
    """发现分类：返回 "hard" / "inferred" / "info"。

    finding 结构：{"type": str, "status_three": str, ...}（与 ptde_finding 行同构）。
    UNKNOWN 状态的硬约束类发现同样阻断（缺失即失败）；已确认（CONFIRMED）的
    硬约束类发现是正常事实，不构成违规。
    """
    ftype = finding.get("type", "")
    if ftype in HARD_TYPES:
        return "hard"
    if ftype in INFERRED_TYPES:
        return "inferred"
    return "info"


class HardConstraintViolation(Exception):
    """A3 硬约束类缺失/失败（或严格模式下推断类）：显式失败，错误带对象名与原因。"""


@dataclass
class PolicyResult:
    """分级裁决结果：失败项 / 告警项 / 严格模式下被阻断项。"""

    failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    blocked_inferred: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and not self.blocked_inferred


def evaluate(findings: list[dict[str, Any]], strict_mode: bool = False) -> PolicyResult:
    """对发现清单执行 A3 分级裁决（不抛异常，供调用方编排）。"""
    result = PolicyResult()
    for f in findings:
        cat = classify(f)
        status = (f.get("status_three") or "").upper()
        if cat == "hard":
            # 硬约束类：UNKNOWN（残缺/失败）→ 显式失败；CONFIRMED 为正常事实
            if status in ("UNKNOWN", "INFERRED", ""):
                result.failures.append(f)
            elif status == "CONFIRMED" and f.get("violation"):
                result.failures.append(f)
        elif cat == "inferred":
            if strict_mode:
                result.blocked_inferred.append(f)
            else:
                result.warnings.append(f)
    return result


def enforce(findings: list[dict[str, Any]], strict_mode: bool = False) -> PolicyResult:
    """evaluate + 显式失败：硬约束类缺失或严格模式推断类 → 抛 HardConstraintViolation。"""
    result = evaluate(findings, strict_mode=strict_mode)
    messages = [_violation_msg(f) for f in result.failures]
    messages += [_violation_msg(f) for f in result.blocked_inferred]
    if messages:
        raise HardConstraintViolation("; ".join(messages))
    return result


def _violation_msg(f: dict[str, Any]) -> str:
    obj = f.get("source_object") or f.get("database_object") or f.get("source_file") or "<unknown>"
    ftype = f.get("type") or "<untyped>"
    reason = f.get("reason") or f.get("detail") or f"status={f.get('status_three')}"
    return f"[{ftype}] {obj}: {reason}"
