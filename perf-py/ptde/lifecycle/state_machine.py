"""Dataset 状态机：确定性程序状态机（实施方案 §3.5 / 总方案 §12.2）。

唯一入口 `transition()`：非法转换抛 `InvalidStateTransition`；
缺操作者/原因抛异常；每次转换写入转换记录（随 Instance 持久化）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from ptde.models.dataset import (
    ALLOWED_TRANSITIONS,
    DatasetInstance,
    DatasetState,
    TransitionRecord,
)

__all__ = ["InvalidStateTransition", "transition"]

# PARTIAL 恢复转换必须携带恢复上下文（实施方案 §3.5 约束 3）
_RECOVERY_REQUIRED: frozenset[tuple[DatasetState, DatasetState]] = frozenset({
    (DatasetState.PARTIAL, DatasetState.GENERATING),
    (DatasetState.PARTIAL, DatasetState.LOADING),
})


class InvalidStateTransition(Exception):
    """非法状态转换（含缺操作者/原因、缺恢复上下文）。"""


def _now() -> datetime:
    return datetime.now(UTC)


def transition(
    instance: DatasetInstance,
    to_state: DatasetState,
    operator: str,
    reason: str,
    manifest_id: str | None = None,
) -> DatasetInstance:
    """执行状态转换：合法则变更 status 并追加转换记录；否则抛 InvalidStateTransition。

    转换记录（from/to/operator/reason/occurred_at[/manifest_id]）追加进
    instance.transition_log（R5+ 落 ptde_dataset_instance.transition_log JSON）。
    """
    if not operator or not operator.strip():
        raise InvalidStateTransition("状态转换必须记录操作者（operator 非空）")
    if not reason or not reason.strip():
        raise InvalidStateTransition("状态转换必须记录原因（reason 非空）")

    from_state = instance.status
    allowed = ALLOWED_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidStateTransition(
            f"非法状态转换：{from_state.value} -> {to_state.value}"
            f"（{from_state.value} 允许的目标态："
            f"{[s.value for s in sorted(allowed, key=lambda x: x.value)] or '无（终态）'}）"
        )
    if (from_state, to_state) in _RECOVERY_REQUIRED and not manifest_id:
        raise InvalidStateTransition(
            f"PARTIAL -> {to_state.value} 恢复转换必须携带恢复上下文 manifest_id"
        )

    record = TransitionRecord(
        from_state=from_state,
        to_state=to_state,
        operator=operator,
        reason=reason,
        occurred_at=_now(),
        manifest_id=manifest_id,
    )
    instance.status = to_state
    instance.transition_log.append(record)
    return instance
