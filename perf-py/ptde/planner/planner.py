"""GenerationPlan 产出与冲突诊断（R3 T3.7，实施方案 §6.2 T3.7；总方案 §6.6）。

编排顺序（对齐 §6.4 骨架）：
1. 目标组合校验（T3.1 targets；互斥组合已在 SpecTargets 模型层拦截）；
2. 工作负载分支（T3.5 workload 收编：有访问事实时按 workload.estimate 推导行数，
   无事实则显式 warning——LLM 增强保持建议定位，不进确定性路径）；
3. 关系规模推导（T3.3 derivation：锚定 explicit → 沿 CONFIRMED 边传播 →
   生产倍率 → 配额修正 + R4 复核锚点）；
4. 分布规划（T3.6：每字段 DistributionPlanEntry + 条件分布对）；
5. 存储估算（T3.4：B_i 实测校准，未经校准显式标记）；
6. 约束求解（T3.2：不可满足 → PlanningConflict 逐条冲突，禁止静默收缩）；
7. 输出 GenerationPlan（warnings 与 conflict_errors 分离；可导出 YAML/JSON）。
"""

from __future__ import annotations

import random
from typing import Any

from ptde.models.entity import RelationModel
from ptde.models.profile import DistributionFit
from ptde.models.snapshot import DatabaseSchemaSnapshot
from ptde.models.spec import (
    DatasetSpec,
    DistributionPlanEntry,
    EntityPlan,
    GenerationPlan,
    RowSource,
)
from ptde.planner import derivation, distribution_planner, solver, storage_estimator
from ptde.planner import workload as wl
from ptde.planner.storage_estimator import CalibratedRowCost
from ptde.planner.targets import check_target_combination

__all__ = ["PLANNER_VERSION", "DatasetPlanner", "build_plan"]

PLANNER_VERSION = "1"


class DatasetPlanner:
    """规模与分布规划器（确定性；算法版本随 warnings 记录可追溯）。"""

    def __init__(
        self,
        spec: DatasetSpec,
        profile: Any | None = None,
        schema: DatabaseSchemaSnapshot | None = None,
        relations: list[RelationModel] | None = None,
        calibration: dict[str, CalibratedRowCost] | None = None,
        rng: random.Random | None = None,
        solver_settings: solver.SolverSettings | None = None,
    ) -> None:
        self.spec = spec
        self.profile = profile
        self.schema = schema
        self.relations = relations
        self.calibration = calibration
        self.rng = rng or random.Random(spec.reproducibility.seed or 0)
        self.solver_settings = solver_settings

    # ---------------------------------------------------------------- 主入口

    def plan(
        self,
        plan_id: str | None = None,
        workload_params: wl.StressParams | None = None,
        workload_demands: list[wl.TableDemand] | None = None,
        workload_weights: list[int] | None = None,
        conditional_pairs: list[tuple[str, str, list[str]]] | None = None,
    ) -> GenerationPlan:
        """规划入口：成功返回 GenerationPlan；不可满足抛 PlanningConflict。"""
        spec = self.spec
        warnings: list[str] = []

        # 1. 目标组合校验（需要外部上下文的冲突项 → 显式失败）
        has_baseline = self._has_production_baseline()
        target_conflicts = check_target_combination(spec, has_baseline)
        if target_conflicts:
            raise solver.PlanningConflict(target_conflicts)

        rows_by_table: dict[str, int] = {}
        sources: dict[str, RowSource] = {}
        wl_derivation: dict[str, str] = {}

        # 2. 工作负载分支（确定性收编路径；LLM 增强不进本路径）
        if spec.targets.workload is not None:
            if workload_params is not None and workload_demands:
                est = wl.estimate(workload_params, workload_demands, workload_weights)
                for table, n in est.rows_by_table.items():
                    rows_by_table[table] = n
                    sources[table] = RowSource.ESTIMATED
                    wl_derivation[table] = (
                        f"工作负载推导（workload.estimate 收编副本，同输入同输出对照 "
                        f"legacy plan_pipeline）：峰值 QPS {est.peak_qps:.2f}、总请求 "
                        f"{est.total_requests}、同表取 max → {n} 行（estimated）"
                    )
                warnings.append(
                    f"工作负载目标分支：workload.estimate 推导行数"
                    f"（峰值 QPS {est.peak_qps:.2f}、总请求 {est.total_requests}；"
                    "同表取 max；legacy 钳制语义保留于 workload 收编副本，"
                    "本行数仅作推导输入，最终规模仍受约束求解显式裁决）"
                )
            else:
                warnings.append(
                    "工作负载目标缺少访问事实输入（workload_params/demands），"
                    "本次未按 workload 推导行数；LLM 增强为建议定位，不进确定性规划路径"
                )

        # 3. 生产倍率换算（生产基线 × 倍率）
        scale_rows = self._compute_scale_rows(warnings)

        # 4. 关系规模推导（explicit 锚定 → 边传播 → 配额修正 + 复核锚点）
        derived = derivation.derive_rows(
            targets_rows=spec.targets.rows,
            schema=self.schema,
            profile=self.profile,
            relations=self.relations,
            scale_rows=scale_rows,
            prelocked_rows=dict(rows_by_table) if rows_by_table else None,
            rng=self.rng,
        )
        for table, ep in derived.plans.items():
            if table in rows_by_table and rows_by_table[table] != ep.rows:
                warnings.append(
                    f"表 {table} 工作负载推导 {rows_by_table[table]} 行与规模推导 "
                    f"{ep.rows} 行不一致，取规模推导结果（关系推导优先，保留锚点）"
                )
            rows_by_table[table] = ep.rows
            sources[table] = ep.source
        warnings.extend(derived.warnings)

        # 5. 分布规划（每字段 DistributionPlanEntry + 条件分布对识别）
        entries = distribution_planner.plan_distributions(self.profile, self.schema)
        pairs = distribution_planner.identify_conditional_pairs(
            self.profile, self.schema, conditional_pairs
        )
        for pair in pairs:
            entries.append(DistributionPlanEntry(
                entity=pair.entity,
                field=pair.dependent,
                distribution=DistributionFit(
                    type="conditional",
                    params={"depends_on": pair.depends_on, "basis": pair.basis},
                    fitted=False,
                ),
                source="stats" if pair.basis == "profile_declared" else "estimated",
            ))

        # 6. 存储估算（B_i 实测校准；未校准显式标记）
        storage_est, storage_warnings = storage_estimator.estimate_storage(
            rows_by_table, self.calibration,
            include_indexes=(spec.targets.storage.include_indexes
                             if spec.targets.storage is not None else True),
        )
        warnings.extend(storage_warnings)

        # 7. 约束求解（不可满足 → PlanningConflict；不做任何静默修正）
        solver.solve(
            rows_by_table,
            storage_est.estimated_total_size_bytes or 0,
            spec.constraints,
            spec.targets.storage,
            self.solver_settings,
        )

        # 8. 组装 GenerationPlan（warnings 与 conflict_errors 分离）
        entities = {}
        for table in sorted(rows_by_table):
            base = derived.plans.get(table)
            entities[table] = EntityPlan(
                entity=table,
                rows=rows_by_table[table],
                source=sources.get(table, RowSource.ESTIMATED),
                derivation=base.derivation if base is not None
                else wl_derivation.get(table, "无推导事件（兜底估计，estimated）"),
                parent_binding=base.parent_binding if base is not None else {},
            )
        warnings.append(
            f"规划算法版本：planner={PLANNER_VERSION} derivation="
            f"{derivation.DERIVATION_ALGORITHM_VERSION} solver="
            f"{solver.SOLVER_ALGORITHM_VERSION} storage="
            f"{storage_estimator.STORAGE_ESTIMATOR_VERSION} distribution="
            f"{distribution_planner.DISTRIBUTION_PLANNER_VERSION}"
        )
        fp = spec.compute_fingerprint()
        plan = GenerationPlan(
            id=plan_id or f"plan-{fp[:16]}",
            dataset_spec_fingerprint=fp,
            target=self._target_label(),
            entities=entities,
            storage=storage_est,
            distribution_plan=entries,
            warnings=warnings,
            conflict_errors=[],  # 规划成功；冲突路径已显式抛 PlanningConflict
        )
        return plan.seal()

    # ---------------------------------------------------------------- 内部

    def _has_production_baseline(self) -> bool:
        if self.schema is None:
            return False
        return any(t.row_estimate >= 0 for t in self.schema.tables)

    def _compute_scale_rows(self, warnings: list[str]) -> dict[str, int]:
        scale = self.spec.targets.scale_factor
        if scale is None or not scale.factors:
            return {}
        out: dict[str, int] = {}
        estimates: dict[str, int] = {}
        if self.schema is not None:
            estimates = {t.name: t.row_estimate for t in self.schema.tables
                         if t.row_estimate >= 0}
        if self.profile is not None:
            for k, ep in (getattr(self.profile, "entities", None) or {}).items():
                est = getattr(ep, "approximate_rows", None)
                if est:
                    estimates.setdefault(k, int(est))
        for table, factor in scale.factors.items():
            baseline = estimates.get(table)
            if baseline is None:
                warnings.append(
                    f"表 {table} 生产规模倍率 {factor} 无生产基线行数，倍率未生效"
                    "（需要 Schema row_estimate 或生产画像）"
                )
                continue
            out[table] = max(1, int(round(baseline * factor)))
        return out

    def _target_label(self) -> str:
        t = self.spec.targets
        kinds = []
        if t.rows:
            kinds.append("row_count")
        if t.scale_factor is not None:
            kinds.append("scale_factor")
        if t.storage is not None:
            kinds.append("storage")
        if t.workload is not None:
            kinds.append("workload")
        if t.auto is not None:
            kinds.append("auto")
        return "combined" if len(kinds) > 1 else (kinds[0] if kinds else "auto")


def build_plan(
    spec: DatasetSpec,
    profile: Any | None = None,
    schema: DatabaseSchemaSnapshot | None = None,
    relations: list[RelationModel] | None = None,
    calibration: dict[str, CalibratedRowCost] | None = None,
    rng: random.Random | None = None,
    plan_id: str | None = None,
    **kwargs: Any,
) -> GenerationPlan:
    """模块级便捷入口：DatasetPlanner(...).plan(...)。"""
    return DatasetPlanner(
        spec, profile=profile, schema=schema, relations=relations,
        calibration=calibration, rng=rng,
    ).plan(plan_id=plan_id, **kwargs)
