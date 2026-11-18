"""PTDE 生命周期层：状态机（R0）；Manifest/清理/数据集管理 + READY 门禁（R6）。"""

from ptde.lifecycle.cleanup import (
    CleanupBlocked,
    CleanupContext,
    CleanupDecision,
    CleanupTier,
    execute_cleanup,
    plan_cleanup,
)
from ptde.lifecycle.dataset_manager import (
    DatasetManager,
    DuplicateDefinitionError,
    ReadyDecision,
    ReadyDenied,
    evaluate_ready,
    require_ready,
)
from ptde.lifecycle.manifest import (
    ManifestIncomplete,
    backfill_from_staging,
    missing_elements,
    require_complete,
    summaries_from_staging,
)
from ptde.lifecycle.state_machine import InvalidStateTransition, transition

__all__ = [
    # R0 状态机
    "InvalidStateTransition",
    "transition",
    # T6.7 Manifest 汇总
    "ManifestIncomplete",
    "backfill_from_staging",
    "missing_elements",
    "require_complete",
    "summaries_from_staging",
    # T6.8 清理策略
    "CleanupBlocked",
    "CleanupContext",
    "CleanupDecision",
    "CleanupTier",
    "execute_cleanup",
    "plan_cleanup",
    # T6.6/T6.9 数据集管理 + READY 门禁
    "DatasetManager",
    "DuplicateDefinitionError",
    "ReadyDecision",
    "ReadyDenied",
    "evaluate_ready",
    "require_ready",
]
