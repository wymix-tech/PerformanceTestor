"""T6.8 清理策略测试（实施方案 §9.2 T6.8 / §9.5 门禁 3；总方案 §12.5/§13.1）。"""

from __future__ import annotations

import pytest

from ptde.lifecycle.cleanup import (
    CleanupBlocked,
    CleanupContext,
    CleanupTier,
    execute_cleanup,
    plan_cleanup,
)
from ptde.loader.gate import DestructiveApproval, GateDenied, SecurityGate


def _approval(target="orders"):
    return DestructiveApproval(
        operation="CLEANUP", target=target, authorized_by="op-1", ticket="T-1")


# ------------------------------------------------------------ 档位选择

def test_dedicated_schema_takes_highest_priority():
    d = plan_cleanup(CleanupContext(env_class="dedicated_test_schema"))
    assert d.tier is CleanupTier.SCHEMA_REBUILD
    assert not d.blocked


def test_dedicated_db_reset():
    d = plan_cleanup(CleanupContext(env_class="dedicated_test_db"))
    assert d.tier is CleanupTier.DATABASE_RESET


def test_shared_without_owner_token_blocked():
    """共享表无归属标记不得猜测归属（§12.5）——阻止清理并报告。"""
    d = plan_cleanup(CleanupContext(env_class="shared_test"))
    assert d.blocked
    assert "所有权标记" in d.reasons[0]


def test_shared_partitioned_uses_partition_clean():
    d = plan_cleanup(CleanupContext(
        env_class="shared_test", owner_token="tok-1", partitioned=True))
    assert d.tier is CleanupTier.PARTITION_CLEAN


def test_shared_large_rows_without_batch_plan_blocked():
    """大规模逐行 DELETE 不作默认方式（§12.5）——无分批计划即阻止。"""
    d = plan_cleanup(CleanupContext(
        env_class="shared_test", owner_token="tok-1", rows_to_clean=2_000_000))
    assert d.blocked
    assert "大规模" in d.reasons[0]


def test_shared_large_rows_with_batch_plan_allowed():
    d = plan_cleanup(CleanupContext(
        env_class="shared_test", owner_token="tok-1",
        rows_to_clean=2_000_000, batch_plan_ready=True))
    assert d.tier is CleanupTier.OWNERSHIP_BATCH_DELETE
    assert not d.blocked


def test_shared_small_rows_ownership_batch_delete():
    d = plan_cleanup(CleanupContext(
        env_class="shared_test", owner_token="tok-1", rows_to_clean=500))
    assert d.tier is CleanupTier.OWNERSHIP_BATCH_DELETE


# ------------------------------------------------------------ 阻止情形

def test_production_cleanup_denied():
    d = plan_cleanup(CleanupContext(env_class="production"))
    assert d.blocked
    assert "禁止清理" in d.reasons[0]


def test_unknown_env_cleanup_denied():
    d = plan_cleanup(CleanupContext(env_class="unknown"))
    assert d.blocked


def test_referenced_by_others_blocked():
    """数据被其他批次引用 → 冲突报告并阻断（§12.5）。"""
    d = plan_cleanup(CleanupContext(
        env_class="dedicated_test_db", referenced_by_others=True))
    assert d.blocked
    assert "不得猜测归属" in d.reasons[0]


def test_unknown_env_class_blocked():
    d = plan_cleanup(CleanupContext(env_class="some_future_env"))
    assert d.blocked


# ------------------------------------------------------------ 执行与审计

def test_execute_blocked_decision_raises_without_touching_gate():
    gate = SecurityGate()
    with pytest.raises(CleanupBlocked):
        execute_cleanup(
            plan_cleanup(CleanupContext(env_class="production")),
            gate, env_class="production", approvals=[], executor=lambda t: 1/0)
    assert gate.audit.to_snapshot() == []


def test_execute_requires_cleanup_approval():
    """CLEANUP 无显式授权 → GateDenied（破坏性操作，§13.3）。"""
    gate = SecurityGate()
    decision = plan_cleanup(CleanupContext(env_class="dedicated_test_db"))
    with pytest.raises(GateDenied):
        execute_cleanup(
            decision, gate, env_class="dedicated_test_db",
            approvals=[], executor=lambda t: None)


def test_execute_with_approval_runs_and_audits():
    """授权后执行成功：决策 + 授权 + 执行三级审计（门禁 3）。"""
    gate = SecurityGate()
    executed = []
    decision = plan_cleanup(CleanupContext(env_class="dedicated_test_db"))
    result = execute_cleanup(
        decision, gate, env_class="dedicated_test_db",
        approvals=[_approval()],
        executor=lambda tier: executed.append(tier))
    assert executed == [CleanupTier.DATABASE_RESET]
    types = [e["event_type"] for e in result["audit"]]
    assert "gate_allowed" in types
    assert "destructive_granted" in types
    assert "cleanup_executed" in types
    assert result["tier"] == "database_reset"


def test_cleanup_decision_requires_approval_by_default():
    d = plan_cleanup(CleanupContext(env_class="dedicated_test_schema"))
    assert d.requires_approval
