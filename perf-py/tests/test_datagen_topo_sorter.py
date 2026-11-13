"""TopoSorter 等价单测：移植 TopoSorterCycleTest 全部 5 个用例。

回归对齐：环依赖报错只列真实环成员（旧实现会把环外依赖方 d 也列进报错）。
"""

import pytest

from datagen.topo_sorter import find_cycle, sort


def deps(*pairs: str) -> dict[str, set[str]]:
    d: dict[str, set[str]] = {}
    for i in range(0, len(pairs) - 1, 2):
        d.setdefault(pairs[i], set()).add(pairs[i + 1])
    return d


def test_cycle_error_reports_only_actual_cycle_members():
    # 环 a→b→c→a；d 依赖 a 但不在环内
    dependencies = deps("a", "b", "b", "c", "c", "a", "d", "a")
    with pytest.raises(ValueError) as ex:
        sort(["a", "b", "c", "d"], dependencies)
    msg = str(ex.value)
    assert "循环依赖" in msg
    # 环上三表必须全部出现
    assert "a" in msg and "b" in msg and "c" in msg
    # 不在环内的 d 不得出现（旧实现缺陷回归）
    assert "d" not in msg, f"环外表 d 不应出现在环报错中: {msg}"


def test_two_node_cycle_detected():
    with pytest.raises(ValueError) as ex:
        sort(["x", "y"], deps("x", "y", "y", "x"))
    assert "x" in str(ex.value) and "y" in str(ex.value)


def test_self_reference_does_not_form_cycle():
    # 自引用边在 sort 内部被跳过（插行时按已插入池处理）
    assert sort(["t", "u"], deps("t", "t", "u", "t")) == ["t", "u"]


def test_acyclic_sorts_parents_first():
    assert sort(["user", "order", "item"], deps("order", "user", "item", "order")) == [
        "user", "order", "item"
    ]


def test_find_cycle_returns_pure_cycle_sequence():
    nodes = {"a", "b", "c", "d"}
    cycle = find_cycle(nodes, deps("a", "b", "b", "c", "c", "a", "d", "a"))
    # 返回纯环序列（首尾相接），不含 d
    assert len(cycle) == 3
    assert set(cycle) == {"a", "b", "c"}


def test_external_dependency_ignored():
    """组外表（不在 tables 内）的依赖被忽略。"""
    assert sort(["child"], deps("child", "external_parent")) == ["child"]
