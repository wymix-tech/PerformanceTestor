"""R3 规模与分布规划器（批次 B3，实施方案 §6）。

模块一览（T3.1～T3.7）：
- targets              目标类型组合校验与单位换算（T3.1）
- solver               约束求解与显式失败 PlanningConflict（T3.2，A1 分轨：无钳制）
- derivation           关系规模推导（离散分布 + 配额修正，确定性，T3.3）
- storage_estimator    存储估算（B_i 实测校准，禁止字段长度相加，T3.4）
- pilot_run            Pilot Run 校准协议（试装载实测 + 扩样修正非线性偏差，T3.4）
- workload             工作负载目标分支（收编 datagen/plan_pipeline.py，T3.5）
- distribution_planner 字段分布参数计划（含条件分布对识别，T3.6）
- planner              GenerationPlan 产出与冲突诊断（T3.7）
"""

from ptde.planner.derivation import (
    DERIVATION_ALGORITHM_VERSION,
    DerivationResult,
    derive_rows,
    distribution_expectation,
)
from ptde.planner.distribution_planner import (
    DISTRIBUTION_PLANNER_VERSION,
    ConditionalPair,
    identify_conditional_pairs,
    plan_distributions,
)
from ptde.planner.planner import DatasetPlanner, build_plan
from ptde.planner.solver import (
    SOLVER_ALGORITHM_VERSION,
    PlanningConflict,
    SolveResult,
    SolverSettings,
    check_constraints,
    solve,
)
from ptde.planner.storage_estimator import (
    STORAGE_ESTIMATOR_VERSION,
    CalibratedRowCost,
    estimate_storage,
)
from ptde.planner.targets import (
    check_target_combination,
    parse_storage_bytes,
    validate_planning_targets,
)
from ptde.planner.workload import (
    Composed,
    EndpointTableFact,
    Estimate,
    JoinRef,
    PlanTable,
    StressParams,
    TableContext,
    TableDemand,
    complete_parents,
    compose,
    edge_confidence,
    estimate,
    parent_rows,
    refresh_seq_starts,
    resolve_rt_baseline,
)

__all__ = [
    "DERIVATION_ALGORITHM_VERSION",
    "DISTRIBUTION_PLANNER_VERSION",
    "SOLVER_ALGORITHM_VERSION",
    "STORAGE_ESTIMATOR_VERSION",
    "CalibratedRowCost",
    "Composed",
    "ConditionalPair",
    "DatasetPlanner",
    "DerivationResult",
    "EndpointTableFact",
    "Estimate",
    "JoinRef",
    "PlanTable",
    "PlanningConflict",
    "SolverSettings",
    "SolveResult",
    "StressParams",
    "TableContext",
    "TableDemand",
    "build_plan",
    "check_constraints",
    "check_target_combination",
    "complete_parents",
    "compose",
    "derive_rows",
    "distribution_expectation",
    "edge_confidence",
    "estimate",
    "estimate_storage",
    "identify_conditional_pairs",
    "parent_rows",
    "parse_storage_bytes",
    "plan_distributions",
    "refresh_seq_starts",
    "resolve_rt_baseline",
    "solve",
    "validate_planning_targets",
]
