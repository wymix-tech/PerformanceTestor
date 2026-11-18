"""T4.7 验收：实体级循环依赖分析（Tarjan SCC，升级 topo_sorter.py 思路）。

- SCC 识别：2 环/3 环/自环/多分量，输出确定性（节点与邻接排序）；
- topo_groups：父实体组先于子实体组（边方向 child → parent）；
- require_acyclic：环显式失败输出环成员（对齐 field_deps 显式失败语义，
  不剔边——legacy break_weakest_cycle_edge 仅对照）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ptde.generator.deps import (
    EntityCycleError,
    build_entity_deps,
    cycles,
    is_self_loop,
    require_acyclic,
    strongly_connected_components,
    topo_groups,
)


def _rel(child: str, parent: str):
    return SimpleNamespace(child_table=child, parent_table=parent)


# ---------------------------------------------------------------- 依赖构建


def test_build_entity_deps_dedup_sorted():
    rels = [
        _rel("orders", "users"),
        _rel("orders", "users"),  # 重复边去重
        _rel("orders", "products"),
        _rel("payments", "orders"),
        None,  # 忽略空项
        "非关系对象（缺 child_table 属性，鸭子类型不产生边）",
    ]
    deps = build_entity_deps(rels)
    assert deps == {
        "orders": ["products", "users"],
        "payments": ["orders"],
    }


def test_build_entity_deps_keeps_self_reference():
    assert build_entity_deps([_rel("t", "t")]) == {"t": ["t"]}


# ---------------------------------------------------------------- Tarjan SCC


def test_scc_singletons_for_acyclic_chain():
    deps = build_entity_deps([_rel("orders", "users"), _rel("payments", "orders")])
    assert strongly_connected_components(deps) == [["users"], ["orders"], ["payments"]]


def test_scc_finds_two_cycle():
    deps = build_entity_deps([_rel("a", "b"), _rel("b", "a")])
    assert strongly_connected_components(deps) == [["a", "b"]]


def test_scc_finds_three_cycle_with_tail():
    # a → b → c → a，外加 a → d（d 是 a 的父实体，环外）
    deps = build_entity_deps([_rel("a", "b"), _rel("b", "c"), _rel("c", "a"), _rel("a", "d")])
    comps = strongly_connected_components(deps)
    # 父实体 d 先于环组产出（生成顺序：父先于子）
    assert comps == [["d"], ["a", "b", "c"]]


def test_scc_multiple_components_deterministic():
    deps = build_entity_deps([
        _rel("a", "b"), _rel("b", "a"),
        _rel("x", "y"), _rel("y", "x"),
    ])
    comps = strongly_connected_components(deps)
    assert comps == [["a", "b"], ["x", "y"]]
    assert strongly_connected_components(deps) == comps


def test_self_loop_detection():
    deps = build_entity_deps([_rel("t", "t")])
    assert is_self_loop("t", deps)
    assert cycles(deps) == [["t"]]
    assert not is_self_loop("orders", {"orders": ["users"], "users": []})


# ---------------------------------------------------------------- 生成顺序分组


def test_topo_groups_parents_first():
    deps = build_entity_deps([_rel("orders", "users"), _rel("payments", "orders")])
    groups = topo_groups(deps)
    assert groups == [["users"], ["orders"], ["payments"]]
    order = [g[0] for g in groups]
    assert order.index("users") < order.index("orders") < order.index("payments")


def test_topo_groups_cycle_as_single_group():
    deps = build_entity_deps([_rel("a", "b"), _rel("b", "c"), _rel("c", "a")])
    assert topo_groups(deps) == [["a", "b", "c"]]


def test_topo_groups_isolated_entities():
    deps = {"users": [], "products": []}
    assert topo_groups(deps) == [["products"], ["users"]]


# ---------------------------------------------------------------- 显式失败


def test_require_acyclic_passes_acyclic():
    deps = build_entity_deps([_rel("orders", "users")])
    require_acyclic(deps)  # 不抛


def test_require_acyclic_fails_with_cycle_members():
    deps = build_entity_deps([_rel("a", "b"), _rel("b", "a")])
    with pytest.raises(EntityCycleError) as exc:
        require_acyclic(deps)
    assert exc.value.cycle == ["a", "b"]
    assert "a -> b" in str(exc.value)
    assert "禁止静默剔边" in str(exc.value)


def test_require_acyclic_fails_on_self_reference():
    deps = build_entity_deps([_rel("category", "category")])
    with pytest.raises(EntityCycleError, match="category"):
        require_acyclic(deps)
