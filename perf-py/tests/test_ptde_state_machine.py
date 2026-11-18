"""R0 验收：Dataset 状态机（实施方案 §3.5 转换表逐格测试 + §3.6 验收 3）。"""

from __future__ import annotations

import pytest

from ptde.lifecycle import InvalidStateTransition, transition
from ptde.models.dataset import ALLOWED_TRANSITIONS, DatasetInstance, DatasetState

ALL_STATES = set(DatasetState)


def _instance() -> DatasetInstance:
    return DatasetInstance(id="inst-1", definition_id="d1")


def test_table_covers_all_states():
    """转换表覆盖全部状态为键；终态无出边。"""
    assert set(ALLOWED_TRANSITIONS) == ALL_STATES
    assert ALLOWED_TRANSITIONS[DatasetState.CLEANED] == frozenset()
    assert ALLOWED_TRANSITIONS[DatasetState.CANCELLED] == frozenset()


def test_happy_path_full_lifecycle():
    """PLANNED→GENERATING→LOADING→VERIFYING→READY→CLEANING→CLEANED 全链路。"""
    inst = _instance()
    for st in (DatasetState.GENERATING, DatasetState.LOADING, DatasetState.VERIFYING,
               DatasetState.READY, DatasetState.CLEANING, DatasetState.CLEANED):
        transition(inst, st, "svc", f"enter {st.value}")
    assert inst.status is DatasetState.CLEANED
    assert [r.to_state for r in inst.transition_log] == [
        DatasetState.GENERATING, DatasetState.LOADING, DatasetState.VERIFYING,
        DatasetState.READY, DatasetState.CLEANING, DatasetState.CLEANED,
    ]


@pytest.mark.parametrize("from_state", sorted(ALL_STATES, key=lambda s: s.value))
@pytest.mark.parametrize("to_state", sorted(ALL_STATES, key=lambda s: s.value))
def test_transition_table_cell_by_cell(from_state, to_state):
    """§3.5 转换表逐格：表内组合可执行并留记录；表外组合抛 InvalidStateTransition。"""
    if to_state in ALLOWED_TRANSITIONS[from_state]:
        kwargs = {"manifest_id": "mf-0"} if from_state is DatasetState.PARTIAL else {}
        transition(_replace := DatasetInstance(id="i", definition_id="d", status=from_state),
                   to_state, "op", "r", **kwargs)
        assert _replace.status is to_state
        rec = _replace.transition_log[-1]
        assert (rec.from_state, rec.to_state) == (from_state, to_state)
        assert rec.operator == "op" and rec.reason == "r" and rec.occurred_at is not None
    else:
        inst = DatasetInstance(id="i", definition_id="d", status=from_state)
        with pytest.raises(InvalidStateTransition, match="非法状态转换"):
            transition(inst, to_state, "op", "r")


def test_partial_recovery_requires_manifest_id():
    """PARTIAL → GENERATING/LOADING 必须携带恢复上下文（上一次 Manifest ID）。"""
    inst = DatasetInstance(id="i", definition_id="d", status=DatasetState.PARTIAL)
    with pytest.raises(InvalidStateTransition, match="manifest_id"):
        transition(inst, DatasetState.GENERATING, "op", "resume")
    transition(inst, DatasetState.GENERATING, "op", "resume", manifest_id="mf-42")
    assert inst.status is DatasetState.GENERATING
    assert inst.transition_log[-1].manifest_id == "mf-42"

    inst2 = DatasetInstance(id="i", definition_id="d", status=DatasetState.PARTIAL)
    transition(inst2, DatasetState.LOADING, "op", "resume-load", manifest_id="mf-1")
    assert inst2.status is DatasetState.LOADING


@pytest.mark.parametrize("operator,reason", [("", "r"), ("   ", "r"), ("op", ""), ("op", "  ")])
def test_operator_and_reason_required(operator, reason):
    """缺操作者/原因的调用一律抛异常（实施方案 §3.5 约束 1）。"""
    inst = _instance()
    with pytest.raises(InvalidStateTransition):
        transition(inst, DatasetState.GENERATING, operator, reason)
    assert inst.status is DatasetState.PLANNED
    assert inst.transition_log == []


def test_terminal_states_have_no_outgoing():
    """CLEANED / CANCELLED 为终态：任何目标态均非法。"""
    for terminal in (DatasetState.CLEANED, DatasetState.CANCELLED):
        for to_state in ALL_STATES:
            inst = DatasetInstance(id="i", definition_id="d", status=terminal)
            with pytest.raises(InvalidStateTransition):
                transition(inst, to_state, "op", "r")
