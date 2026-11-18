"""T6.6/T6.9 数据集管理 + READY 门禁测试（实施方案 §9.2/§9.4/§9.5；总方案 §18.4）。"""

from __future__ import annotations

import pytest

from ptde.lifecycle.cleanup import CleanupDecision
from ptde.lifecycle.dataset_manager import (
    DatasetManager,
    DuplicateDefinitionError,
    ReadyDenied,
    evaluate_ready,
    require_ready,
)
from ptde.lifecycle.state_machine import InvalidStateTransition
from ptde.models.dataset import (
    DatasetState,
    LayerKind,
    LayerResult,
    ValidationReport,
    ValidationStatus,
)
from ptde.models.spec import DatasetSpec, ExecutionManifest, ShardSummary, SpecTargets


def _spec(name="ds", rows=100):
    return DatasetSpec(name=name, targets=SpecTargets(rows={"users": rows}))


def _layer(kind, status):
    return LayerResult(layer=kind, status=status)


def _full_report(all_status=ValidationStatus.PASS):
    return ValidationReport(instance_id="inst", layers=[
        _layer(k, all_status) for k in LayerKind
    ])


def _full_manifest(instance_id="inst"):
    return ExecutionManifest(
        id="m1", instance_id=instance_id, seed=42,
        generator_version="0.5.0", adapter_version="ad-1",
        adapter_capability_snapshot={"product": "mysql"},
        model_fingerprints={"spec": "a", "plan": "b", "schema": "c", "profile": "d"},
        shard_summaries=[ShardSummary(shard_id="s1", entity="users",
                                      generated_rows=100, loaded_rows=100,
                                      status="loaded")],
        actual_id_ranges={"users": {"start": 1, "end": 100}},
    )


def _ok_cleanup():
    return CleanupDecision(tier=None, blocked=False, reasons=[])


# ------------------------------------------------------------ T6.6 Definition 管理

def test_register_definition_fingerprints_spec():
    mgr = DatasetManager()
    spec = _spec()
    d = mgr.register_definition("order_ds", "1", "订单数据集", spec, created_by="u1")
    assert d.spec_fingerprint == spec.compute_fingerprint()
    assert d.created_by == "u1"


def test_duplicate_definition_rejected():
    mgr = DatasetManager()
    mgr.register_definition("order_ds", "1", "订单数据集", _spec())
    with pytest.raises(DuplicateDefinitionError):
        mgr.register_definition("order_ds", "1", "另一名", _spec(name="x"))


def test_same_key_new_version_allowed():
    """同 def_key 可多 version（版本化）。"""
    mgr = DatasetManager()
    mgr.register_definition("order_ds", "1", "v1", _spec())
    mgr.register_definition("order_ds", "2", "v2", _spec(rows=200))
    assert mgr.list_versions("order_ds") == ["1", "2"]


def test_get_definition_missing_raises():
    mgr = DatasetManager()
    with pytest.raises(KeyError):
        mgr.get_definition("nope", "1")


# ------------------------------------------------------------ T6.6 Instance 管理

def _setup_manager():
    mgr = DatasetManager()
    d = mgr.register_definition("order_ds", "1", "订单数据集", _spec())
    return mgr, d


def test_same_definition_multiple_instances():
    """同一 Definition 可建多 Instance（复用语义）。"""
    mgr, d = _setup_manager()
    i1 = mgr.create_instance(d, environment_id="env-test-1")
    i2 = mgr.create_instance(d, environment_id="env-test-2")
    assert i1.id != i2.id
    assert i1.status is DatasetState.PLANNED
    assert len(mgr.list_instances("order_ds")) == 2


def test_transition_instance_uses_state_machine():
    mgr, d = _setup_manager()
    inst = mgr.create_instance(d, environment_id="env-test-1")
    mgr.transition_instance(inst.id, DatasetState.GENERATING,
                            operator="op", reason="开工")
    assert mgr.get_instance(inst.id).status is DatasetState.GENERATING


def test_illegal_transition_rejected():
    mgr, d = _setup_manager()
    inst = mgr.create_instance(d, environment_id="env-test-1")
    with pytest.raises(InvalidStateTransition):
        mgr.transition_instance(inst.id, DatasetState.READY,
                                operator="op", reason="跳步")


def test_transition_requires_operator_and_reason():
    mgr, d = _setup_manager()
    inst = mgr.create_instance(d, environment_id="env-test-1")
    with pytest.raises(InvalidStateTransition):
        mgr.transition_instance(inst.id, DatasetState.GENERATING,
                                operator="", reason="")


# ------------------------------------------------------------ T6.9 READY 门禁

def _verifying_instance(mgr, d):
    inst = mgr.create_instance(d, environment_id="env-test-1",
                               target_database=None)
    inst.owner_token = "tok-1"
    mgr.transition_instance(inst.id, DatasetState.GENERATING,
                            operator="op", reason="开工")
    mgr.transition_instance(inst.id, DatasetState.LOADING,
                            operator="op", reason="装载")
    mgr.transition_instance(inst.id, DatasetState.VERIFYING,
                            operator="op", reason="验证")
    return inst


def test_ready_allowed_when_all_eight_satisfied():
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    decision = evaluate_ready(
        inst, _full_report(), _full_manifest(instance_id=inst.id), _ok_cleanup())
    assert decision.allowed
    assert len(decision.checks) == 8
    require_ready(decision)


def test_layer_fail_blocks_ready():
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    report = _full_report()
    for lr in report.layers:
        if lr.layer is LayerKind.RELATIONSHIP:
            lr.status = ValidationStatus.FAIL
    decision = evaluate_ready(
        inst, report, _full_manifest(instance_id=inst.id), _ok_cleanup())
    assert not decision.allowed
    assert any(c.name == "ready.relationship" for c in decision.failed())
    with pytest.raises(ReadyDenied):
        require_ready(decision)


def test_distribution_warn_degradable_by_default():
    """分布层 WARN 为可降级项（默认放行，随报告留痕）。"""
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    report = _full_report()
    for lr in report.layers:
        if lr.layer is LayerKind.DISTRIBUTION:
            lr.status = ValidationStatus.WARN
    decision = evaluate_ready(
        inst, report, _full_manifest(instance_id=inst.id), _ok_cleanup())
    assert decision.allowed
    assert any("可降级项" in c.detail for c in decision.checks)


def test_business_warn_not_degradable_by_default():
    """业务不变量层 WARN 不在默认可降级集 → 拒绝（更严层不放行）。"""
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    report = _full_report()
    for lr in report.layers:
        if lr.layer is LayerKind.BUSINESS:
            lr.status = ValidationStatus.WARN
    decision = evaluate_ready(
        inst, report, _full_manifest(instance_id=inst.id), _ok_cleanup())
    assert not decision.allowed


def test_missing_layer_blocks_ready():
    """五层缺一 → 报告不完整 → 拒绝（不视为通过）。"""
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    report = _full_report()
    report.layers = [lr for lr in report.layers if lr.layer is not LayerKind.DB_READY]
    decision = evaluate_ready(
        inst, report, _full_manifest(instance_id=inst.id), _ok_cleanup())
    assert not decision.allowed


def test_unfinished_shards_block_ready():
    """分片未完成/行数不符 → 条件 2 FAIL。"""
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    manifest = _full_manifest(instance_id=inst.id)
    manifest.shard_summaries[0].loaded_rows = 99
    decision = evaluate_ready(inst, _full_report(), manifest, _ok_cleanup())
    assert not decision.allowed
    assert any(c.name == "ready.shards_complete" for c in decision.failed())


def test_manifest_missing_elements_block_ready():
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    manifest = _full_manifest(instance_id=inst.id)
    manifest.seed = None
    decision = evaluate_ready(inst, _full_report(), manifest, _ok_cleanup())
    assert not decision.allowed
    assert any(c.name == "ready.artifacts_complete" for c in decision.failed())


def test_instance_id_link_mismatch_blocks_ready():
    """instance → manifest 链路 ID 不一致 → 条件 7 FAIL（可追溯性）。"""
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    decision = evaluate_ready(
        inst, _full_report(), _full_manifest(instance_id="other"), _ok_cleanup())
    assert not decision.allowed


def test_cleanup_blocked_blocks_ready():
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    decision = evaluate_ready(
        inst, _full_report(), _full_manifest(instance_id=inst.id),
        CleanupDecision(blocked=True, reasons=["无归属标记"]))
    assert not decision.allowed


def test_missing_owner_token_blocks_ready():
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    inst.owner_token = None
    decision = evaluate_ready(
        inst, _full_report(), _full_manifest(instance_id=inst.id), _ok_cleanup())
    assert not decision.allowed


def test_finalize_to_ready_via_state_machine():
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    decision = evaluate_ready(
        inst, _full_report(), _full_manifest(instance_id=inst.id), _ok_cleanup())
    mgr.finalize_verification(inst.id, decision, operator="op")
    assert mgr.get_instance(inst.id).status is DatasetState.READY


def test_finalize_to_failed_records_reason():
    mgr, d = _setup_manager()
    inst = _verifying_instance(mgr, d)
    decision = evaluate_ready(
        inst, _full_report(), _full_manifest(instance_id=inst.id),
        CleanupDecision(blocked=True, reasons=["无归属标记"]))
    mgr.finalize_verification(inst.id, decision, operator="op")
    failed_inst = mgr.get_instance(inst.id)
    assert failed_inst.status is DatasetState.FAILED
    assert "无归属标记" in failed_inst.transition_log[-1].reason
