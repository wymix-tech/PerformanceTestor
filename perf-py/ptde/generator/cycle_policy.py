"""循环依赖显式安全策略（R4 T4.7，实施方案 §7.2；总方案 §7.6 + 风险表）。

**废除 legacy `break_weakest_cycle_edge` 静默剔边语义**（A1 分轨：legacy
本体保留不动仅对照）；环内实体集合的安全策略逐条判定：

1. 自引用环（单实体自环）→ 带内自引用，`SELF_REF_MAX_DEPTH` 深度上限
   （继承 `datagen/default_engine.py` 语义：插行过程中动态追加已插入 ID）；
2. 环内全部 FK 列可空 → 可空回填（两阶段：先插 NULL 再回填）；
3. 目标库支持延迟约束 → 延迟约束（事务内回填前不校验）；
4. 显式批准关闭约束的分阶段装载 → 分阶段装载（总方案风险表：关闭约束
   导致脏数据 → 特殊策略需显式批准并完整验证）；
5. 无安全方式 → `CyclePolicyError` 阻止执行并逐边输出原因（禁止剔边）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from ptde.models.snapshot import DatabaseSchemaSnapshot

SELF_REF_MAX_DEPTH = 5  # 继承 datagen/default_engine.py 语义


class CycleStrategy(StrEnum):
    """环依赖安全策略（判定优先序见模块 docstring）。"""

    SELF_REFERENCE = "self_reference"
    NULLABLE_BACKFILL = "nullable_backfill"
    DEFERRED_CONSTRAINT = "deferred_constraint"
    STAGED_LOAD = "staged_load"


class CyclePolicyError(Exception):
    """环无安全装载方式：阻止执行并输出原因（显式失败，不剔边）。"""

    def __init__(self, cycle: list[str], reasons: list[str]):
        self.cycle = cycle
        self.reasons = reasons
        super().__init__(
            f"实体环 {' -> '.join(cycle)} 无安全装载方式，阻止执行："
            + "；".join(reasons)
            + "（循环依赖显式失败，禁止静默剔边——"
              "legacy break_weakest_cycle_edge 保留不动仅对照）"
        )


@dataclass(frozen=True)
class CycleFkEdge:
    """环内 FK 边的可空性输入（由 Schema 快照列信息投影）。"""

    child_table: str
    child_column: str
    nullable: bool


def project_fk_edges(
    snapshot: DatabaseSchemaSnapshot,
    cycle_members: Iterable[str],
) -> list[CycleFkEdge]:
    """Schema 快照 → 环内 ``CycleFkEdge`` 投影（B4.2 遗留问题 2）。

    - 边方向与 ``deps`` 一致（child → parent）；只投影 child_table ∈ 环成员的
      声明式 FK，parent 是否属于环交由 ``resolve_cycle`` 过滤；
    - 复合 FK 按 child_columns 逐列展开为单列粒度的 ``CycleFkEdge``；
    - 环成员不在快照中 → 不产出该成员的边（交由 ``resolve_cycle`` 按
      「信息不足」显式失败，A3：不得伪装可装载）；
    - 表在快照中但 FK 子列缺失 → ``ValueError`` 显式失败（快照自相矛盾）。
    """
    members = set(cycle_members)
    tables = {t.name: t for t in snapshot.tables}
    edges: list[CycleFkEdge] = []
    for table_name in sorted(members):
        table = tables.get(table_name)
        if table is None:
            continue
        columns = {c.name: c for c in table.columns}
        for fk in table.foreign_keys:
            for col_name in fk.child_columns:
                col = columns.get(col_name)
                if col is None:
                    raise ValueError(
                        f"实体 {table_name} 的 FK 子列 {col_name} 在 Schema 快照中缺失"
                        "（快照自相矛盾，显式失败——可空性信息不足不得伪装）"
                    )
                edges.append(CycleFkEdge(table_name, col_name, col.nullable))
    return edges


@dataclass(frozen=True)
class CycleResolution:
    """单个环的安全策略判定结果。"""

    cycle: tuple[str, ...]
    strategy: CycleStrategy
    steps: tuple[str, ...] = ()
    depth: int | None = None  # 仅 self_reference：SELF_REF_MAX_DEPTH

    @property
    def requires_backfill(self) -> bool:
        return self.strategy in (
            CycleStrategy.NULLABLE_BACKFILL, CycleStrategy.DEFERRED_CONSTRAINT,
        )


def resolve_cycle(
    cycle_members: Iterable[str],
    fk_edges: Iterable[CycleFkEdge],
    supports_deferred_constraints: bool = False,
    allow_constraint_disabled: bool = False,
) -> CycleResolution:
    """单环安全策略逐条判定（优先序见模块 docstring）。

    fk_edges：环内实体的 FK 边（child_table ∈ 环成员）；环成员缺少 FK 边
    可空性信息时按「信息不足」显式失败（不得伪装可装载——A3）。
    """
    members = sorted(set(cycle_members))
    if not members:
        raise ValueError("环成员为空（显式失败）")

    # 1. 自引用环：带内自引用 + 深度上限（legacy 语义继承）
    if len(members) == 1:
        return CycleResolution(
            cycle=(members[0],),
            strategy=CycleStrategy.SELF_REFERENCE,
            depth=SELF_REF_MAX_DEPTH,
            steps=(
                f"自引用 {members[0]}：插行过程中动态追加已插入 ID"
                f"（自引用深度上限 SELF_REF_MAX_DEPTH={SELF_REF_MAX_DEPTH}）",
            ),
        )

    edges = [e for e in fk_edges if e.child_table in set(members)]
    child_tables = {e.child_table for e in edges}
    missing = [m for m in members if m not in child_tables]
    nullable_all = bool(edges) and all(e.nullable for e in edges)

    # 2. 可空回填：环内全部 FK 列可空
    if not missing and nullable_all:
        return CycleResolution(
            cycle=tuple(members),
            strategy=CycleStrategy.NULLABLE_BACKFILL,
            steps=(
                f"阶段一：插入 {'、'.join(members)}（环内 FK 列置 NULL）",
                "阶段二：按已插入 ID 回填环内 FK 列（"
                + "、".join(f"{e.child_table}.{e.child_column}" for e in edges) + "）",
            ),
        )

    # 3. 延迟约束：目标库支持事务内延迟校验
    if not missing and supports_deferred_constraints:
        return CycleResolution(
            cycle=tuple(members),
            strategy=CycleStrategy.DEFERRED_CONSTRAINT,
            steps=(
                f"阶段一：事务内插入 {'、'.join(members)}（FK 列先置 NULL，"
                "约束延迟到提交）",
                "阶段二：事务内回填环内 FK 列后提交（延迟约束校验）",
            ),
        )

    # 4. 分阶段装载：需显式批准关闭约束（总方案风险表）
    if not missing and allow_constraint_disabled:
        return CycleResolution(
            cycle=tuple(members),
            strategy=CycleStrategy.STAGED_LOAD,
            steps=(
                f"阶段一：关闭 FK 约束装载 {'、'.join(members)}"
                "（已获显式批准，须完整验证——总方案风险表）",
                "阶段二：回填环内 FK 列并恢复约束",
            ),
        )

    # 5. 无安全方式：阻止执行并逐边输出原因
    reasons: list[str] = []
    for e in edges:
        if not e.nullable:
            reasons.append(f"{e.child_table}.{e.child_column} NOT NULL（不可空回填）")
    for m in missing:
        reasons.append(f"实体 {m} 缺少环内 FK 列可空性信息（不得伪装可装载）")
    if not supports_deferred_constraints:
        reasons.append("目标库不支持延迟约束")
    if not allow_constraint_disabled:
        reasons.append("未批准关闭约束的分阶段装载（关闭约束需显式批准并完整验证）")
    raise CyclePolicyError(members, reasons)


def resolve_cycles(
    cycles_of: Iterable[Iterable[str]],
    fk_edges: Iterable[CycleFkEdge],
    **capabilities: bool,
) -> dict[tuple[str, ...], CycleResolution]:
    """批量判定（deps.cycles 产出 → 每环 Resolution；任一环无安全方式即抛错）。"""
    edges = list(fk_edges)
    resolutions: dict[tuple[str, ...], CycleResolution] = {}
    for cycle_members in cycles_of:
        members = list(cycle_members)
        resolutions[tuple(sorted(members))] = resolve_cycle(members, edges, **capabilities)
    return resolutions
