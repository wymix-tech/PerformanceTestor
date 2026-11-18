"""T6.8 清理策略（实施方案 §9.2 T6.8；总方案 §12.5）。

清理优先级（§12.5 原文）：独立 Schema 重建 > 独立库清空 > 分区清理 >
所有权标记分批清理。

硬约束（A3，显式失败不静默放行）：
- **共享表无归属标记不得猜测归属**——无 owner_token 即阻止清理并报告；
- 数据被其他批次引用（referenced_by_others）→ 冲突报告并阻断；
- **大规模逐行 DELETE 不作默认方式**——超阈值且无分批计划 → 阻止；
- 生产库/无法识别环境禁止清理（§13.1，R5 T5.9 口径延续）；
- 清理属破坏性操作（CLEANUP）：执行须过 SecurityGate.evaluate_cleanup
  显式授权 + 审计（R5 T5.9 组件复用，不另起一套）。
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from typing import Any

from pydantic import Field

from ptde.loader.gate import DestructiveApproval, SecurityGate
from ptde_common.model_base import VersionedModel

__all__ = [
    "CleanupBlocked",
    "CleanupContext",
    "CleanupDecision",
    "CleanupTier",
    "LARGE_DELETE_ROWS",
    "execute_cleanup",
    "plan_cleanup",
]


class CleanupTier(enum.StrEnum):
    """清理档位（§12.5 优先级从高到低）。"""

    SCHEMA_REBUILD = "schema_rebuild"  # 独立 Schema 重建
    DATABASE_RESET = "database_reset"  # 独立库清空
    PARTITION_CLEAN = "partition_clean"  # 分区清理
    OWNERSHIP_BATCH_DELETE = "ownership_batch_delete"  # 所有权标记分批删除


# 大规模逐行 DELETE 阈值（§12.5：不作默认方式；须显式分批计划）
LARGE_DELETE_ROWS = 1_000_000


class CleanupContext(VersionedModel):
    """清理决策输入（环境/所有权/规模事实，由调用方如实提供）。"""

    env_class: str  # SecurityGate.EnvClass value（生产/未知直接阻止）
    owner_token: str | None = None  # instance.owner_token（共享表清理凭据）
    partitioned: bool = False  # 目标表按分区组织（可分区清理）
    referenced_by_others: bool = False  # 数据被其他批次引用（冲突）
    rows_to_clean: int = Field(default=0, ge=0)
    batch_plan_ready: bool = False  # 分批删除计划就绪（批次上限/审计）


class CleanupDecision(VersionedModel):
    """清理决策：档位 + 阻止原因（决策入审计，执行须过门禁授权）。"""

    blocked: bool = False
    tier: CleanupTier | None = None
    reasons: list[str] = Field(default_factory=list)
    requires_approval: bool = True  # CLEANUP 属破坏性操作（恒须授权）


class CleanupBlocked(Exception):
    """清理被阻止（原因清单随异常返回；决策已可入审计）。"""


def plan_cleanup(ctx: CleanupContext) -> CleanupDecision:
    """按 §12.5 优先级选档；阻止情形逐条留痕（不猜测归属）。"""
    reasons: list[str] = []

    # 环境硬门禁（§13.1：生产库禁止清理；无法识别环境禁止操作）
    env = (ctx.env_class or "").lower()
    if env in ("production", "unknown"):
        reasons.append(
            f"环境类别 {env} 禁止清理（§13.1：生产库禁止执行数据清理；"
            "无法识别的环境禁止操作）"
        )
        return CleanupDecision(blocked=True, reasons=reasons)

    # 归属冲突（§12.5：数据被其他批次引用应阻止清理并报告冲突）
    if ctx.referenced_by_others:
        reasons.append(
            "数据被其他批次引用（§12.5：不得猜测归属——冲突报告并阻断清理）"
        )
        return CleanupDecision(blocked=True, reasons=reasons)

    # 档位选择（优先级从高到低）
    if env == "dedicated_test_schema":
        return CleanupDecision(tier=CleanupTier.SCHEMA_REBUILD, reasons=reasons)
    if env == "dedicated_test_db":
        return CleanupDecision(tier=CleanupTier.DATABASE_RESET, reasons=reasons)

    # shared_test：独立于共享数据的清理路径（分区清理 > 所有权标记分批）
    if env == "shared_test":
        if not ctx.owner_token:
            reasons.append(
                "共享表清理须所有权标记（§12.5：无批次标记/归属信息不得猜测"
                "归属——阻止清理并报告）"
            )
            return CleanupDecision(blocked=True, reasons=reasons)
        if ctx.partitioned:
            reasons.append("共享环境按分区清理（所有权标记归属 + 分区粒度）")
            return CleanupDecision(tier=CleanupTier.PARTITION_CLEAN, reasons=reasons)
        if ctx.rows_to_clean > LARGE_DELETE_ROWS and not ctx.batch_plan_ready:
            reasons.append(
                f"待清理 {ctx.rows_to_clean} 行超过大规模阈值 {LARGE_DELETE_ROWS}"
                "（§12.5：大规模逐行 DELETE 不作默认方式——须分批计划或改档）"
            )
            return CleanupDecision(blocked=True, reasons=reasons)
        reasons.append("共享环境按所有权标记分批清理（owner_token 归属核对）")
        return CleanupDecision(tier=CleanupTier.OWNERSHIP_BATCH_DELETE, reasons=reasons)

    reasons.append(
        f"环境类别 {env!r} 不在已知清理档位（dedicated_test_schema/"
        "dedicated_test_db/shared_test）——阻止清理"
    )
    return CleanupDecision(blocked=True, reasons=reasons)


def execute_cleanup(
    decision: CleanupDecision,
    gate: SecurityGate,
    *,
    env_class: str,
    approvals: list[DestructiveApproval],
    executor: Callable[[CleanupTier], Any],
) -> dict[str, Any]:
    """执行清理：阻止即抛 CleanupBlocked；门禁拒绝即抛 GateDenied；执行入审计。

    返回审计快照（决策 + 授权 + 执行三级事件，§9.5 门禁 3「清理动作有审计」）。
    """
    if decision.blocked:
        raise CleanupBlocked(
            f"清理被阻止：{decision.reasons}（不得绕过决策——破坏性操作须显式处置）"
        )
    # CLEANUP 属破坏性操作：显式授权 + 审计（R5 T5.9 门禁复用）
    gate.evaluate_cleanup(
        declared_env_class=env_class, approvals=approvals,
    ).require_passed()
    tier = decision.tier
    assert tier is not None
    executor(tier)
    gate.audit.record(
        "cleanup_executed", tier=tier.value, reasons=list(decision.reasons),
    )
    return {"audit": gate.audit.to_snapshot(), "tier": tier.value}
