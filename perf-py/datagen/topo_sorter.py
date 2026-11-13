"""拓扑排序与环检测（Kahn 算法）：移植自 engine/TopoSorter.java。

决定表组生成顺序：父表（被依赖方）先插，子表后插；出现环时报错并定位环中表名。
回归对齐：环依赖报错只列真实环成员（DFS 三色标记），不再「列出全部未入序表」。
"""

from __future__ import annotations

from collections import deque


def sort(tables, dependencies: dict[str, set[str]]) -> list[str]:
    """按依赖排序（父先子后）；组外依赖与自引用忽略；环依赖抛 ValueError。"""
    in_degree: dict[str, int] = {}
    children: dict[str, set[str]] = {}
    for t in tables:
        if t not in in_degree:
            in_degree[t] = 0
    for child, parents in dependencies.items():
        if child not in in_degree:
            continue
        for parent in parents:
            if parent not in in_degree or parent == child:
                continue  # 组外依赖或自引用（自引用不影响排序，插行时按已插入池处理）
            children.setdefault(parent, set()).add(child)
            in_degree[child] += 1

    ready = deque(t for t, d in in_degree.items() if d == 0)
    order: list[str] = []
    while ready:
        t = ready.popleft()
        order.append(t)
        for c in children.get(t, ()):  # noqa: B007
            in_degree[c] -= 1
            if in_degree[c] == 0:
                ready.append(c)

    if len(order) != len(in_degree):
        unfinished = [t for t in in_degree if t not in set(order)]
        # DFS 提取真实环路（仅环中表），替代"列出全部未入序表"的误导性报错
        cycle = find_cycle(unfinished, dependencies)
        target = (", ".join(unfinished) if not cycle
                  else " -> ".join(cycle) + " -> " + cycle[0])
        raise ValueError("表组存在循环依赖，无法确定生成顺序: " + target)
    return order


def find_cycle(nodes, dependencies: dict[str, set[str]]) -> list[str]:
    """从未完成节点中提取一条真实环路（DFS 三色标记，沿 child → parent 依赖方向）。

    返回环路节点序列（如 [a, b, c] 表示 a→b→c→a）；无环返回空列表。
    """
    state: dict[str, int] = {}  # 0/缺失=白 1=灰(栈上) 2=黑(完成)
    for start in nodes:
        if start in state:
            continue
        path: list[str] = []
        if _dfs(start, nodes, dependencies, state, path):
            return path
    return []


def _dfs(node, nodes, dependencies, state, path) -> bool:
    state[node] = 1
    path.append(node)
    for nxt in dependencies.get(node, set()):
        if nxt not in nodes or nxt == node:
            continue  # 组外依赖/自引用不构成组内环
        s = state.get(nxt)
        if s == 1:
            # 命中灰节点：截取 path 中 nxt 起始的完整环（丢弃环外 DFS 前缀）
            from_idx = path.index(nxt)
            if from_idx >= 0:
                cycle = path[from_idx:]
                path.clear()
                path.extend(cycle)
                return True
            return False
        if s is None and _dfs(nxt, nodes, dependencies, state, path):
            return True
    path.pop()
    state[node] = 2
    return False
