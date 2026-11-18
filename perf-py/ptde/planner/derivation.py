"""关系规模推导（R3 T3.3，实施方案 §6.4 骨架第 1～3 步；总方案 §6.3）。

确定性推导算法（版本记录于 DERIVATION_ALGORITHM_VERSION）：
1. 锚定显式目标表（rows → source=explicit）；
2. 生产倍率表（scale_factor × 生产基线 row_estimate → source=production）；
3. 沿 CONFIRMED 关系边传播：
   - 子表规模 = Σ 父实体数 × 每父子实体数分布期望（zero_child_ratio 入模）；
   - 反向：父表规模 = 子表行数 / 每父子实体数期望（总方案 §6.3：先定义
     用户订单量分布再求和，零订单用户占比入模，不做"除以固定平均值"假装精确）；
4. 配额修正：期望总量与目标行数偏差 > 容差时按分布保形修正（修正事件记录进
   derivation 说明）；小规模表用注入的 random.Random 做确定性采样复核；
5. 每表结果带 source(explicit|production|derived|estimated) 与推导说明，
   并在 parent_binding 留 R4 生成后复核锚点（验收门禁 4：数量容差内可复核）。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from ptde.models.entity import EvidenceStatus, RelationModel
from ptde.models.profile import DistributionFit, RelationCardinalityProfile
from ptde.models.snapshot import DatabaseSchemaSnapshot
from ptde.models.spec import EntityPlan, RowSource

__all__ = [
    "DERIVATION_ALGORITHM_VERSION",
    "SIMULATION_MAX_PARENTS",
    "DerivationEdge",
    "DerivationResult",
    "collect_edges",
    "derive_rows",
    "distribution_expectation",
    "sample_children_total",
]

DERIVATION_ALGORITHM_VERSION = "1"

DEFAULT_TOLERANCE = 0.05  # 配额修正容差（5%）
DEFAULT_CHILDREN_PER_PARENT = 10.0  # 无分布时兜底期望（对齐 legacy AVG_CHILDREN 语义）
DEFAULT_ESTIMATED_TABLE_ROWS = 1_000  # 无任何依据表的兜底行数（source=estimated）
SIMULATION_MAX_PARENTS = 10_000  # 采样复核的父实体数上限（超此规模用解析期望）


@dataclass(frozen=True)
class DerivationEdge:
    """关系推导边（child → parent，多对一方向）。"""

    child_table: str
    parent_table: str
    child_column: str


@dataclass
class DerivationResult:
    """推导结果：每表 EntityPlan + 告警 + 修正事件 + 算法版本。"""

    plans: dict[str, EntityPlan] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    correction_events: list[str] = field(default_factory=list)
    algorithm_version: str = DERIVATION_ALGORITHM_VERSION


# ---------------------------------------------------------------- 边收集


def collect_edges(
    schema: DatabaseSchemaSnapshot | None,
    relations: list[RelationModel] | None = None,
) -> list[DerivationEdge]:
    """汇总关系边：Schema 声明外键（CONFIRMED 事实）优先；RelationModel 仅取 CONFIRMED。

    按 (child, parent, child_column) 去重、保序（确定性）。
    """
    edges: list[DerivationEdge] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(e: DerivationEdge) -> None:
        key = (e.child_table.lower(), e.parent_table.lower(), e.child_column.lower())
        if key in seen or key[0] == key[1]:
            return
        seen.add(key)
        edges.append(e)

    if schema is not None:
        for ts in schema.tables:
            for fk in ts.foreign_keys:
                child_col = fk.child_columns[0] if fk.child_columns else ""
                _add(DerivationEdge(ts.name, fk.parent_table, child_col))
    for rel in relations or []:
        if rel.status == EvidenceStatus.CONFIRMED:
            _add(
                DerivationEdge(
                    rel.child_table, rel.parent_table, rel.child_column
                )
            )
    return edges


# ---------------------------------------------------------------- 分布期望


def distribution_expectation(
    dist: DistributionFit | None,
    zero_child_ratio: float | None = None,
    fallback: float = DEFAULT_CHILDREN_PER_PARENT,
) -> float:
    """每父子实体数期望（确定性解析期望）。

    - WeightedCategorical：Σ 值×权重；Uniform：(min+max)/2；Normal：mean；
      LogNormal：exp(mu+sigma²/2)；其余取 params.mean；无分布 → fallback。
    - zero_child_ratio：分布按"有子实体父实体"条件拟合（B2 采集口径），
      期望 × (1 - zero_child_ratio) 入模（总方案 §6.3 零订单用户占比）。
    """
    base: float | None = None
    if dist is not None:
        p = dist.params or {}
        t = (dist.type or "").lower()
        if t in ("weighted_categorical", "weightedcategorical"):
            values, weights = p.get("values"), p.get("weights")
            if isinstance(values, list) and isinstance(weights, list) \
                    and len(values) == len(weights) and weights:
                base = sum(float(v) * float(w) for v, w in zip(values, weights, strict=True))
        elif t == "uniform":
            if p.get("min") is not None and p.get("max") is not None:
                base = (float(p["min"]) + float(p["max"])) / 2.0
        elif t == "normal":
            if p.get("mean") is not None:
                base = float(p["mean"])
        elif t == "lognormal":
            if p.get("mu") is not None and p.get("sigma") is not None:
                base = math.exp(float(p["mu"]) + float(p["sigma"]) ** 2 / 2.0)
        elif t in ("zipf", "skewed") and p.get("mean") is not None:
            base = float(p["mean"])
        if base is None and p.get("mean") is not None:
            base = float(p["mean"])
    if base is None:
        base = fallback
    if zero_child_ratio is not None:
        base *= 1.0 - float(zero_child_ratio)
    return max(0.0, base)


def sample_one(dist: DistributionFit | None, rng: random.Random, fallback: float
               ) -> float:
    """单次采样（确定性：rng 为注入的 random.Random 实例）。"""
    if dist is None:
        return fallback
    p = dist.params or {}
    t = (dist.type or "").lower()
    if t in ("weighted_categorical", "weightedcategorical"):
        values, weights = p.get("values"), p.get("weights")
        if isinstance(values, list) and isinstance(weights, list) \
                and len(values) == len(weights) and weights:
            total = sum(float(w) for w in weights)
            pick = rng.random() * total
            acc = 0.0
            for v, w in zip(values, weights, strict=True):
                acc += float(w)
                if pick <= acc:
                    return float(v)
            return float(values[-1])
    if t == "uniform" and p.get("min") is not None and p.get("max") is not None:
        lo, hi = float(p["min"]), float(p["max"])
        if lo == int(lo) and hi == int(hi):
            return float(rng.randint(int(lo), int(hi)))
        return rng.uniform(lo, hi)
    if t == "normal" and p.get("mean") is not None:
        sigma = float(p.get("sigma", p.get("std", 0.0)) or 0.0)
        return max(0.0, rng.gauss(float(p["mean"]), sigma))
    if t == "lognormal" and p.get("mu") is not None:
        return math.exp(rng.gauss(float(p["mu"]), float(p.get("sigma", 0.0) or 0.0)))
    if p.get("mean") is not None:
        return float(p["mean"])
    return fallback


def sample_children_total(
    dist: DistributionFit | None,
    n_parents: int,
    rng: random.Random,
    zero_child_ratio: float | None = None,
    fallback: float = DEFAULT_CHILDREN_PER_PARENT,
) -> int:
    """父实体数 n_parents 的子实体总量采样（确定性采样，注入 random.Random）。"""
    total = 0.0
    for _ in range(max(0, n_parents)):
        if zero_child_ratio is not None and rng.random() < float(zero_child_ratio):
            continue
        total += sample_one(dist, rng, fallback)
    return int(round(total))


# ---------------------------------------------------------------- 关联基数画像


def _find_cardinality(
    profile: Any,
    child: str,
    parent: str,
) -> RelationCardinalityProfile | None:
    """从 UnifiedDataProfile 取 (child, parent) 关联基数画像（表名不区分大小写）。

    画像挂载实体可能缺子表实体（B2 merger 按子表挂载，但来源画像可能只覆盖
    部分实体），故跨实体扫描匹配 (child_table, parent_table)。
    """
    entities = getattr(profile, "entities", None) or {}
    ep = entities.get(child)
    if ep is None:
        lowered = {k.lower(): v for k, v in entities.items()}
        ep = lowered.get(child.lower())
    for cand in ([ep] if ep is not None else []) + list(entities.values()):
        for rel in getattr(cand, "relation_cardinalities", []) or []:
            if rel.child_table.lower() == child.lower() \
                    and rel.parent_table.lower() == parent.lower():
                return rel
    return None


def _cardinality_expectation(
    card: RelationCardinalityProfile | None,
) -> tuple[float, str]:
    """(期望, 说明)；无画像 → 兜底期望 + 假设说明（显式 estimated，不伪装统计）。"""
    if card is None:
        return DEFAULT_CHILDREN_PER_PARENT, (
            f"无关联基数画像，按兜底期望 {DEFAULT_CHILDREN_PER_PARENT} 假设"
            "（source=estimated，不得伪装为生产统计）"
        )
    exp = distribution_expectation(
        card.child_per_parent_distribution, card.zero_child_ratio
    )
    note = "按关联基数画像分布期望"
    if card.zero_child_ratio is not None:
        note += f"（零子实体父实体占比 {card.zero_child_ratio} 入模）"
    return exp, note


# ---------------------------------------------------------------- 主推导


def derive_rows(
    targets_rows: dict[str, int],
    schema: DatabaseSchemaSnapshot | None,
    profile: Any = None,
    relations: list[RelationModel] | None = None,
    scale_rows: dict[str, int] | None = None,
    prelocked_rows: dict[str, int] | None = None,
    rng: random.Random | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> DerivationResult:
    """规模推导主入口（确定性）。

    - targets_rows：显式锚定行数（source=explicit）；
    - scale_rows：生产倍率已换算行数（source=production，由 planner 计算传入）；
    - prelocked_rows：前置锚定行数（如 workload.estimate 推导输入，source 仍 estimated）；
    - profile：UnifiedDataProfile（关联基数/zero_child_ratio 来源，B2 产出）；
    - rng：确定性采样注入（None 时内部以 seed=0 固定）。
    """
    rng = rng or random.Random(0)
    result = DerivationResult()
    notes: dict[str, list[str]] = {}
    rows: dict[str, int] = {}

    def _note(table: str, text: str) -> None:
        notes.setdefault(table, []).append(text)

    # 1. 锚定显式目标表
    for table, n in targets_rows.items():
        rows[table] = n
        _note(table, f"用户显式指定目标行数 {n}（explicit）")

    # 1b. 前置锚定（工作负载推导输入等）：不改变 source 标注
    for table, n in (prelocked_rows or {}).items():
        if table in rows:
            continue
        rows[table] = n
        _note(table, f"工作负载推导锚定 {n} 行（estimated，规模推导输入）")

    # 2. 生产倍率表
    for table, n in (scale_rows or {}).items():
        if table in rows:
            result.warnings.append(
                f"表 {table} 同时命中显式行数与生产倍率，以显式行数为准（倍率忽略）"
            )
            continue
        rows[table] = n
        _note(table, f"生产基线行数 × 用户倍率 = {n}（production）")

    edges = collect_edges(schema, relations)

    # 3. 沿 CONFIRMED 关系边传播（不动点迭代；同表多候选取 max 并记录）
    changed = True
    while changed:
        changed = False
        candidates: dict[str, tuple[int, str]] = {}
        for edge in edges:
            child, parent = edge.child_table, edge.parent_table
            if parent in rows and child not in rows:
                card = _find_cardinality(profile, child, parent)
                exp, exp_note = _cardinality_expectation(card)
                cand = int(round(rows[parent] * exp))
                text = (
                    f"沿关系边 {parent}→{child}（{edge.child_column}）推导："
                    f"{rows[parent]} × 每父子实体数期望 {exp:.2f} = {cand}"
                    f"（{exp_note}；derived）"
                )
                if child not in candidates or cand > candidates[child][0]:
                    candidates[child] = (cand, text)
            elif child in rows and parent not in rows:
                card = _find_cardinality(profile, child, parent)
                exp, exp_note = _cardinality_expectation(card)
                if exp <= 0:
                    continue
                cand = max(1, int(round(rows[child] / exp)))
                text = (
                    f"由子表 {child}（{edge.child_column}）反向推导："
                    f"{rows[child]} ÷ 每父子实体数期望 {exp:.2f} = {cand}"
                    f"（{exp_note}；总方案 §6.3 用户订单量分布先于用户数推导；derived）"
                )
                if parent not in candidates or cand > candidates[parent][0]:
                    candidates[parent] = (cand, text)
        for table, (cand, text) in candidates.items():
            rows[table] = cand
            _note(table, text)
            changed = True

    # 4. 剩余未知表：Schema 生产行数参考（仍标 estimated）或兜底行数
    universe: list[str] = []
    if schema is not None:
        universe.extend(t.name for t in schema.tables)
    if profile is not None:
        universe.extend(k for k in (getattr(profile, "entities", None) or {})
                        if k not in universe)
    universe.extend(t for t in rows if t not in universe)
    row_estimates: dict[str, int] = {}
    if schema is not None:
        row_estimates = {t.name: t.row_estimate for t in schema.tables}
    for table in universe:
        if table in rows:
            continue
        est = row_estimates.get(table)
        if est is not None and est >= 0:
            rows[table] = est
            _note(
                table,
                f"无显式目标与可推导关系边，取 Schema 生产行数参考 {est}"
                "（estimated，R4 生成前应复核）",
            )
        else:
            rows[table] = DEFAULT_ESTIMATED_TABLE_ROWS
            _note(
                table,
                f"无显式目标、无可推导关系边与生产行数参考，按兜底行数 "
                f"{DEFAULT_ESTIMATED_TABLE_ROWS}（estimated，不得伪装为生产统计）",
            )

    # 5. 配额修正：显式锚定子表的期望总量与目标偏差 > 容差时按分布保形修正
    for table, target in targets_rows.items():
        incoming = [e for e in edges if e.child_table.lower() == table.lower()
                    and e.parent_table in rows]
        if not incoming:
            continue
        edge = incoming[0]
        card = _find_cardinality(profile, table, edge.parent_table)
        exp, _ = _cardinality_expectation(card)
        expected_total = rows[edge.parent_table] * exp
        if expected_total <= 0:
            continue
        deviation = abs(expected_total - target) / target
        corrected = exp
        if deviation > tolerance:
            corrected = exp * (target / expected_total)
            event = (
                f"配额修正事件（表 {table}）：期望总量 {expected_total:.0f} 与目标 "
                f"{target} 偏差 {deviation:.1%} 超容差 {tolerance:.0%}，"
                f"每父子实体数期望按分布保形修正 {exp:.4f} → {corrected:.4f}"
            )
            result.correction_events.append(event)
            _note(table, event)
        # 确定性采样复核（小规模表）：注入 rng，采样总量记录进说明
        parent_rows = rows[edge.parent_table]
        if parent_rows <= SIMULATION_MAX_PARENTS:
            sampled = sample_children_total(
                card.child_per_parent_distribution if card else None,
                parent_rows,
                rng,
                card.zero_child_ratio if card else None,
            )
            _note(
                table,
                f"采样复核（{parent_rows} 个父实体，注入随机源）：采样总量 {sampled}"
                f"（R4 生成后按此锚点复核，容差 {tolerance:.0%}）",
            )

    # 6. 组装 EntityPlan：source 标注 + 推导说明 + R4 复核锚点
    prelocked = set((prelocked_rows or {}).keys())
    for table in sorted(rows):
        source = RowSource.ESTIMATED
        if table in targets_rows:
            source = RowSource.EXPLICIT
        elif table in (scale_rows or {}):
            source = RowSource.PRODUCTION
        elif table in prelocked:
            source = RowSource.ESTIMATED  # 前置锚定不改标注（推导输入）
        elif any(e.child_table.lower() == table.lower()
                 and e.parent_table in rows for e in edges) \
                or any(e.parent_table.lower() == table.lower()
                       and e.child_table in rows for e in edges):
            source = RowSource.DERIVED
        parent_binding: dict[str, Any] = {}
        for edge in edges:
            if edge.child_table.lower() == table.lower() and edge.parent_table in rows:
                card = _find_cardinality(profile, table, edge.parent_table)
                exp, _ = _cardinality_expectation(card)
                parent_binding[edge.parent_table] = {
                    "child_column": edge.child_column,
                    "expected_children_per_parent": round(exp, 4),
                    "expected_total": rows[table],
                    "tolerance": tolerance,
                    "review_hint": "R4 生成后按 expected_total 容差内复核关联数量",
                }
        derivation = "；".join(notes.get(table, ["无推导事件"]))
        result.plans[table] = EntityPlan(
            entity=table,
            rows=rows[table],
            source=source,
            derivation=derivation,
            parent_binding=parent_binding,
        )
    return result
