"""实体级循环依赖分析（R4 T4.7，实施方案 §7.2；升级 `datagen/topo_sorter.py` 思路）。

Tarjan SCC（迭代实现，深图不递归溢出）；节点与邻接均排序，输出确定性。
边方向 = child → parent（child 依赖 parent），因此 Tarjan 的 SCC 产出顺序
天然满足「父实体组先于子实体组」的生成顺序。

环判定复用 field_deps 的显式失败语义（A1/A3）——本模块只做分析不剔边；
安全策略逐条判定见 `cycle_policy.py`（废除 legacy `break_weakest_cycle_edge`
静默剔边语义，A1 分轨：legacy 本体保留不动仅对照）。

自引用（自环）单独识别：交由 cycle_policy 以 SELF_REF_MAX_DEPTH 深度上限
语义处理（继承 datagen/default_engine.py）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


class EntityCycleError(Exception):
    """实体依赖图存在环：显式失败（对齐 A1/A3，不做静默剔边）。"""

    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(
            f"实体依赖图存在环：{' -> '.join(cycle)}"
            "（循环依赖必须走 cycle_policy 安全策略逐条判定，"
            "禁止静默剔边——legacy break_weakest_cycle_edge 保留不动仅对照）"
        )


def build_entity_deps(relations: Iterable) -> dict[str, list[str]]:
    """汇总实体依赖：child → [parents]（去重排序，确定性）。

    消费 `RelationModel` 形态（child_table/parent_table 四元组，鸭子类型）；
    自引用（child == parent）保留为自环，由 cycle_policy 单独处理。
    """
    deps: dict[str, list[str]] = {}
    for rel in relations or []:
        if rel is None:
            continue
        child = getattr(rel, "child_table", None)
        parent = getattr(rel, "parent_table", None)
        if not child or not parent:
            continue
        lst = deps.setdefault(child, [])
        if parent not in lst:
            lst.append(parent)
    return {k: sorted(v) for k, v in deps.items()}


def strongly_connected_components(deps: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """Tarjan SCC（迭代实现）；每个 SCC 内部排序，组件顺序确定性。"""
    graph: dict[str, list[str]] = {n: sorted(set(v)) for n, v in deps.items()}
    # 目标节点（父实体）可能不在 deps 键内，补齐为孤立节点（先取键快照再扩充）
    for targets in list(graph.values()):
        for t in targets:
            graph.setdefault(t, [])

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    result: list[list[str]] = []

    for root in sorted(graph):
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            neighbors = graph[node]
            descended = False
            while pi < len(neighbors):
                nxt = neighbors[pi]
                pi += 1
                work[-1] = (node, pi)
                if nxt not in index:
                    work.append((nxt, 0))
                    descended = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if descended:
                continue
            if low[node] == index[node]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                result.append(sorted(comp))
            work.pop()
            if work:
                parent_node = work[-1][0]
                low[parent_node] = min(low[parent_node], low[node])
    return result


def is_self_loop(node: str, deps: Mapping[str, Iterable[str]]) -> bool:
    return node in set(deps.get(node, ()))


def cycles(deps: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """全部环（SCC 长度 > 1，或含自环的单节点组）。"""
    return [
        comp for comp in strongly_connected_components(deps)
        if len(comp) > 1 or is_self_loop(comp[0], deps)
    ]


def topo_groups(deps: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """生成顺序分组：父实体组先于子实体组（环不在此处展开，交由 cycle_policy）。"""
    return strongly_connected_components(deps)


def require_acyclic(deps: Mapping[str, Iterable[str]]) -> None:
    """无环校验：存在环 → EntityCycleError（显式失败，输出环成员）。"""
    found = cycles(deps)
    if found:
        raise EntityCycleError(found[0])
