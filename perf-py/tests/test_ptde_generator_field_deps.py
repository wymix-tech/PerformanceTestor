"""T4.3 验收：条件分布字段依赖图（消费 B3 条件对；拓扑决定字段生成顺序）。

环依赖显式失败不剔边（对齐 A1/A3；legacy break_weakest_cycle_edge 保留不动仅对照）。
"""

from __future__ import annotations

import pytest

from ptde.generator.field_deps import (
    FieldDependencyError,
    build_field_deps,
    generation_orders,
    topological_order,
)
from ptde.models.profile import DistributionFit
from ptde.models.spec import DistributionPlanEntry


def _conditional_entry(entity: str, field: str, depends_on: list[str],
                       basis: str = "heuristic") -> DistributionPlanEntry:
    """planner.py 折叠形态：DistributionFit(type="conditional",
    params={"depends_on": [...], "basis": ...})。"""
    return DistributionPlanEntry(
        entity=entity, field=field,
        distribution=DistributionFit(
            type="conditional", params={"depends_on": depends_on, "basis": basis},
            fitted=False,
        ),
        source="stats" if basis == "profile_declared" else "estimated",
    )


# ---------------------------------------------------------------- B3 条件对消费


def test_build_deps_from_plan_entries():
    """消费 planner.py 折叠形态：conditional 对 → entity → {field: deps}。"""
    entries = [
        _conditional_entry("orders", "paid_at", ["status"]),
        _conditional_entry("orders", "ship_at", ["paid_at"]),
        # 非条件分布型不产生依赖边
        DistributionPlanEntry(
            entity="orders", field="amount",
            distribution=DistributionFit(type="uniform",
                                         params={"min": 0, "max": 100}),
            source="estimated",
        ),
    ]
    deps = build_field_deps(entries)
    assert deps == {"orders": {"paid_at": ["status"], "ship_at": ["paid_at"]}}


def test_build_deps_from_conditional_pairs():
    """消费 distribution_planner 原始形态 ConditionalPair（鸭子类型）。"""

    class Pair:
        def __init__(self, entity, dependent, depends_on, basis):
            self.entity = entity
            self.dependent = dependent
            self.depends_on = depends_on
            self.basis = basis

    deps = build_field_deps([Pair("orders", "paid_at", ["status"], "heuristic")])
    assert deps == {"orders": {"paid_at": ["status"]}}


def test_deps_dedupe_and_empty_depends_skipped():
    entries = [
        _conditional_entry("orders", "paid_at", ["status", "status", ""]),
        _conditional_entry("orders", "remark", []),
    ]
    deps = build_field_deps(entries)
    assert deps["orders"]["paid_at"] == ["status"]
    assert deps["orders"]["remark"] == []


# ---------------------------------------------------------------- 拓扑顺序


def test_topological_order_conditional_dependency():
    """P(paid_at|status) 型：status 先于 paid_at 生成。"""
    order = topological_order({"paid_at": ["status"], "amount": []})
    assert order.index("status") < order.index("paid_at")
    assert set(order) == {"paid_at", "status", "amount"}


def test_topological_order_chain_and_determinism():
    """链式依赖（status → paid_at → ship_at）逐级在前；同层按名称排序（确定性）。"""
    deps = {"ship_at": ["paid_at"], "paid_at": ["status"], "amount": [],
            "buyer": ["account"], "account": []}
    order1 = topological_order(deps)
    order2 = topological_order(dict(deps))
    assert order1 == order2
    assert order1.index("status") < order1.index("paid_at") < order1.index("ship_at")
    assert order1.index("account") < order1.index("buyer")


def test_topological_order_includes_rule_generated_dependency():
    """依赖字段不在分布计划里（status 由规则生成）也纳入排序。"""
    order = topological_order({"paid_at": ["status"]})
    assert order == ["status", "paid_at"]


# ---------------------------------------------------------------- 环依赖显式失败


def test_cycle_fails_explicitly_two_nodes():
    """a→b→a 环：显式失败且环成员入报错信息；绝不返回剔边后的顺序。"""
    with pytest.raises(FieldDependencyError) as exc:
        topological_order({"paid_at": ["status"], "status": ["paid_at"]})
    assert "paid_at" in str(exc.value) and "status" in str(exc.value)
    assert "禁止静默剔边" in str(exc.value)


def test_cycle_fails_explicitly_self_reference():
    """自引用（a→a）：显式失败。"""
    with pytest.raises(FieldDependencyError):
        topological_order({"paid_at": ["paid_at"]})


def test_cycle_fails_explicitly_three_nodes():
    """三节点环（a→b→c→a）：无任何安全拓扑时阻断，输出环成员。"""
    with pytest.raises(FieldDependencyError) as exc:
        topological_order({"a": ["c"], "b": ["a"], "c": ["b"]})
    assert set(exc.value.cycle) == {"a", "b", "c"}


def test_generation_orders_multi_entity():
    """多实体条件对：每实体独立拓扑；无依赖实体不产出。"""
    entries = [
        _conditional_entry("orders", "paid_at", ["status"], "profile_declared"),
        _conditional_entry("users", "last_login", ["state"]),
    ]
    orders_map = generation_orders(entries)
    assert orders_map["orders"] == ["status", "paid_at"]
    assert orders_map["users"] == ["state", "last_login"]
    assert set(orders_map) == {"orders", "users"}


def test_generation_orders_empty_input():
    assert generation_orders([]) == {}
