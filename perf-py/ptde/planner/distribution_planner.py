"""字段分布参数规划（R3 T3.6，实施方案 §6.2 T3.6；总方案 §6.6/§7.3）。

为每字段产出 DistributionPlanEntry（类型 + 参数 + 来源；R4 采样器消费）：
- 来源对齐 B2 统计口径：有统计画像分布/Top-K → source="stats"；
  无统计依据走显式默认分布 → source="estimated"（DistributionFit.fitted=False，
  不得伪装为生产统计）；
- 条件分布对（如 status↔paid_at）识别为字段依赖图输入（R4 field_deps 消费）：
  ① 画像声明的 conditional 分布；② 调用方显式配置；③ 表内启发式候选
  （类别列 + 带 NULL 的时间列），记录 basis 供追溯。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ptde.models.profile import DistributionFit, UnifiedDataProfile
from ptde.models.snapshot import DatabaseSchemaSnapshot
from ptde.models.spec import DistributionPlanEntry
from ptde_common.model_base import VersionedModel

__all__ = [
    "DISTRIBUTION_PLANNER_VERSION",
    "ConditionalPair",
    "identify_conditional_pairs",
    "plan_distributions",
]

DISTRIBUTION_PLANNER_VERSION = "1"

CATEGORICAL_TYPES = {"weighted_categorical", "weightedcategorical"}
TEMPORAL_TYPES = {"temporal"}
MAX_HEURISTIC_CATEGORICAL_DISTINCT = 32
MIN_HEURISTIC_NULL_RATIO = 0.01
STATUS_NAME_SUFFIXES = ("status", "state", "flag")


class ConditionalPair(VersionedModel):
    """条件分布对：dependent 字段的取值依赖 depends_on 字段（R4 字段依赖图输入）。"""

    entity: str
    dependent: str  # 如 paid_at
    depends_on: list[str] = Field(default_factory=list)  # 如 ["status"]
    basis: str  # profile_declared / configured / heuristic


def _default_fit(type_name: str) -> DistributionFit:
    """无统计依据时的显式默认分布（fitted=False，source=estimated）。"""
    t = (type_name or "").lower()
    if "bigint" in t or "int" in t:
        return DistributionFit(type="uniform", params={"min": 1, "max": 1_000_000},
                               fitted=False)
    if "decimal" in t or "numeric" in t or "double" in t or "float" in t:
        return DistributionFit(type="uniform",
                               params={"min": 0, "max": 10_000, "precision": 2},
                               fitted=False)
    if "char" in t or "text" in t:
        return DistributionFit(type="uniform", params={"min_length": 1, "max_length": 64},
                               fitted=False)
    return DistributionFit(type="uniform", params={}, fitted=False)


def _stats_fit(col_profile: Any) -> DistributionFit | None:
    """从列画像取统计分布；仅 Top-K 时合成 WeightedCategorical（脱敏桶占比）。"""
    dist = getattr(col_profile, "distribution", None)
    if dist is not None and dist.type:
        return dist
    top_k = getattr(col_profile, "top_k", None) or []
    if top_k:
        total = sum(e.count for e in top_k)
        if total > 0:
            return DistributionFit(
                type="weighted_categorical",
                params={"values": [e.bucket for e in top_k],
                        "weights": [e.count / total for e in top_k]},
                fitted=True,
            )
    return None


def plan_distributions(
    profile: UnifiedDataProfile | None,
    schema: DatabaseSchemaSnapshot | None = None,
    overrides: dict[str, DistributionFit] | None = None,
) -> list[DistributionPlanEntry]:
    """为每字段产出分布参数计划（确定性；表清单以 Schema 优先、画像补充）。

    overrides：键 "table.column"，值 DistributionFit（用户目标覆盖统计，
    对齐总方案 §4.4「字段值分布可被 DatasetSpec 目标覆盖」）。
    """
    entries: list[DistributionPlanEntry] = []
    profile_entities = (profile.entities if profile is not None else None) or {}
    tables: list[str] = []
    if schema is not None:
        tables.extend(t.name for t in schema.tables)
    tables.extend(k for k in profile_entities if k not in tables)

    for table in sorted(tables):
        ep = profile_entities.get(table) or profile_entities.get(table.lower())
        columns: list[tuple[str, str]] = []
        if schema is not None:
            ts = next((t for t in schema.tables if t.name == table), None)
            if ts is not None:
                columns = [(c.name, c.type_name) for c in ts.columns]
        if not columns and ep is not None:
            columns = [(name, "") for name in (ep.columns or {})]
        elif not columns:
            continue

        for col_name, type_name in columns:
            key = f"{table}.{col_name}"
            override = (overrides or {}).get(key)
            if override is not None:
                entries.append(DistributionPlanEntry(
                    entity=table, field=col_name, distribution=override, source="estimated"
                ))
                continue
            col_profile = (ep.columns.get(col_name) if ep is not None else None) \
                or (ep.columns.get(col_name.lower()) if ep is not None else None)
            fit = _stats_fit(col_profile) if col_profile is not None else None
            if fit is not None:
                entries.append(DistributionPlanEntry(
                    entity=table, field=col_name, distribution=fit, source="stats"
                ))
            else:
                entries.append(DistributionPlanEntry(
                    entity=table, field=col_name, distribution=_default_fit(type_name),
                    source="estimated",
                ))
    return entries


def identify_conditional_pairs(
    profile: UnifiedDataProfile | None,
    schema: DatabaseSchemaSnapshot | None = None,
    configured: list[tuple[str, str, list[str]]] | None = None,
) -> list[ConditionalPair]:
    """识别条件分布对（R4 field_deps 字段依赖图输入）。

    configured 元素为 (entity, dependent, depends_on)。
    识别顺序：画像声明 → 显式配置 → 启发式候选（记录 basis，R4 可选择性采纳）。
    """
    pairs: list[ConditionalPair] = []
    seen: set[tuple[str, str]] = set()

    def _add(entity: str, dependent: str, depends_on: list[str], basis: str) -> None:
        key = (entity.lower(), dependent.lower())
        if key in seen or not depends_on:
            return
        seen.add(key)
        pairs.append(ConditionalPair(entity=entity, dependent=dependent,
                                     depends_on=depends_on, basis=basis))

    profile_entities = (profile.entities if profile is not None else None) or {}
    tables: list[str] = []
    if schema is not None:
        tables.extend(t.name for t in schema.tables)
    tables.extend(k for k in profile_entities if k not in tables)

    for table in sorted(tables):
        ep = profile_entities.get(table) or profile_entities.get(table.lower())
        cols = dict((ep.columns or {}) if ep is not None else {})
        lowered = {k.lower(): v for k, v in cols.items()}

        # ① 画像声明的 conditional 分布
        for name, cp in cols.items():
            dist = getattr(cp, "distribution", None)
            if dist is not None and (dist.type or "").lower() == "conditional":
                dep = dist.params.get("depends_on") or []
                dep = dep if isinstance(dep, list) else [dep]
                _add(table, name, [str(d) for d in dep], "profile_declared")

        # ② 显式配置
        for entity, dependent, depends_on in configured or []:
            if entity.lower() == table.lower():
                _add(entity, dependent, depends_on, "configured")

        # ③ 启发式候选：类别列（status/state/flag 后缀、小基数）+ 带 NULL 的时间列
        categorical: dict[str, Any] = {}
        temporal: dict[str, Any] = {}
        for name, cp in lowered.items():
            dist = getattr(cp, "distribution", None)
            t = (getattr(dist, "type", "") or "").lower()
            if t in CATEGORICAL_TYPES:
                categorical[name] = cp
            elif t in TEMPORAL_TYPES:
                temporal[name] = cp
        for cat_name, cp in categorical.items():
            if not any(cat_name.endswith(s) for s in STATUS_NAME_SUFFIXES):
                continue
            if (getattr(cp, "distinct_count", None) is not None
                    and cp.distinct_count > MAX_HEURISTIC_CATEGORICAL_DISTINCT):
                continue
            for tm_name, tp in temporal.items():
                if tm_name == cat_name:
                    continue
                null_ratio = getattr(tp, "null_ratio", None) or 0.0
                if null_ratio >= MIN_HEURISTIC_NULL_RATIO:
                    _add(table, tm_name, [cat_name], "heuristic")
    return pairs
