"""条件分布字段依赖图（R4 T4.3，实施方案 §7.2；总方案 §7.2 字段关联分布/§7.3）。

消费 B3 条件分布对（两种形态均可）：
- DistributionPlanEntry：distribution.type == "conditional" +
  params["depends_on"]（planner.py 折叠形态，basis 随 params 携带）；
- ConditionalPair：dependent/depends_on 字段（distribution_planner 原始形态）。

字段生成顺序由依赖图拓扑决定（P(paid_at|status) 型：status 先生成）；
环依赖显式失败——禁止静默剔边（A1 分轨：legacy break_weakest_cycle_edge
保留不动仅对照；A3 分级显式：硬约束冲突不得伪装成功）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence


class FieldDependencyError(Exception):
    """字段依赖图存在环：显式失败（对齐 A1/A3，不做静默剔边）。"""

    def __init__(self, cycle: list[str], entity: str | None = None):
        self.cycle = cycle
        prefix = f"实体 {entity} " if entity else ""
        super().__init__(
            f"{prefix}字段依赖图存在环：{' -> '.join(cycle)}"
            "（环依赖显式失败，禁止静默剔边——legacy break_weakest_cycle_edge"
            " 保留不动仅对照）"
        )


def build_field_deps(entries: Iterable) -> dict[str, dict[str, list[str]]]:
    """汇总字段依赖：entity → {dependent 字段: [依赖字段]}（去重保序，确定性）。"""
    graph: dict[str, dict[str, list[str]]] = {}
    for item in entries or []:
        if item is None:
            continue
        dist = getattr(item, "distribution", None)
        if dist is not None and getattr(item, "field", None) is not None:
            # DistributionPlanEntry 形态（仅条件分布型产生依赖边）
            if (getattr(dist, "type", "") or "").lower() != "conditional":
                continue
            entity = item.entity
            field = item.field
            depends = dist.params.get("depends_on") or []
        elif getattr(item, "dependent", None) is not None:
            # ConditionalPair 形态
            entity = item.entity
            field = item.dependent
            depends = item.depends_on or []
        else:
            continue
        field_deps = graph.setdefault(entity, {})
        existing = field_deps.setdefault(field, [])
        for d in depends:
            if not d:
                continue
            d = str(d)
            if d not in existing:
                existing.append(d)
    return graph


def topological_order(deps: Mapping[str, Sequence[str]]) -> list[str]:
    """拓扑排序：依赖字段先于依赖它的字段生成（同层按名称排序，确定性）。

    环（含自引用）→ FieldDependencyError（显式失败，不剔边）。
    依赖字段可能不在分布计划里（如 status 由规则生成），一并纳入排序。
    """
    fields: set[str] = set(deps)
    for ds in deps.values():
        fields.update(ds)
    remaining = {f: list(deps.get(f, [])) for f in fields}
    order: list[str] = []
    ready = sorted(f for f in fields if not remaining[f])
    while ready:
        node = ready.pop(0)
        order.append(node)
        for f, ds in remaining.items():
            if node in ds:
                ds.remove(node)
                if not ds and f not in order and f not in ready:
                    ready.append(f)
        ready = sorted(set(ready))
    if len(order) < len(fields):
        cycle_nodes = sorted(f for f in fields if f not in order)
        raise FieldDependencyError(cycle_nodes)
    return order


def generation_orders(entries: Iterable) -> dict[str, list[str]]:
    """入口：B3 条件对清单 → 每实体字段生成顺序（依赖图拓扑决定）。"""
    graph = build_field_deps(entries)
    return {entity: topological_order(deps) for entity, deps in graph.items()}
