"""约束求解与显式失败（R3 T3.2，实施方案 §6.4 骨架第 4 步；总方案 §6.4）。

A1 分轨：新管线**无 100 万钳制**、不设 max_rows——目标不可满足时抛
`PlanningConflict`（含逐条冲突说明），**禁止静默降低行数或改变分布**；
legacy `default_engine.py`/`plan_pipeline.py` 的钳制与缩减行为保留不动（对照管线）。

确定性约束检查（算法版本随结果记录）：存储 ≤ max_staging_storage 与存储目标、
生成时间 ≤ max_generation_time（确定性吞吐模型）、连接数 ≤ max_database_connections。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ptde.models.spec import ResourceConstraints, StorageTarget
from ptde.planner.targets import parse_storage_bytes

__all__ = [
    "SOLVER_ALGORITHM_VERSION",
    "PlanningConflict",
    "SolverSettings",
    "SolveResult",
    "check_constraints",
    "solve",
]

SOLVER_ALGORITHM_VERSION = "1"

# 生成耗时估算的确定性吞吐模型（行/秒；R4 Pilot Run 实测后校准替换）
DEFAULT_GENERATION_THROUGHPUT_ROWS_PER_SEC = 100_000.0
# 规划期装载并发占用（R5 分片并发规划前，按降级通道保守取值）
DEFAULT_PLANNED_LOADER_CONNECTIONS = 4


@dataclass(frozen=True)
class SolverSettings:
    """求解器设定：吞吐模型与规划期连接占用（记录进求解结果可追溯）。"""

    generation_throughput_rows_per_sec: float = DEFAULT_GENERATION_THROUGHPUT_ROWS_PER_SEC
    planned_loader_connections: int = DEFAULT_PLANNED_LOADER_CONNECTIONS


class PlanningConflict(Exception):
    """规模规划不可满足：逐条冲突说明；不静默降级（A1）。

    错误信息包含全部冲突条目；conflicts 属性供程序化消费
    （GenerationPlan.conflict_errors 同源，warnings 与 conflict_errors 分离）。
    """

    def __init__(self, conflicts: list[str]) -> None:
        self.conflicts = list(conflicts)
        joined = "\n".join(f"  {i}. {c}" for i, c in enumerate(self.conflicts, start=1))
        super().__init__(
            f"规模规划不可满足，共 {len(self.conflicts)} 条冲突（禁止静默降低行数或"
            f"改变分布）：\n{joined}"
        )


@dataclass
class SolveResult:
    """求解结果：满足全部约束时的规模/时间/存储与设定快照。"""

    total_rows: int
    estimated_generation_time_ms: float
    estimated_storage_bytes: int
    throughput_rows_per_sec: float
    algorithm_version: str = SOLVER_ALGORITHM_VERSION
    conflicts: list[str] = field(default_factory=list)


def check_constraints(
    rows_by_table: dict[str, int],
    storage_total_bytes: int,
    constraints: ResourceConstraints,
    storage_target: StorageTarget | None = None,
    settings: SolverSettings | None = None,
) -> list[str]:
    """逐条约束检查（确定性；返回全部冲突说明，不做任何行数修正）。"""
    s = settings or SolverSettings()
    conflicts: list[str] = []
    total_rows = sum(rows_by_table.values())

    if constraints.max_staging_storage_bytes is not None \
            and storage_total_bytes > constraints.max_staging_storage_bytes:
        conflicts.append(
            f"估算总存储 {storage_total_bytes} 字节超过约束 max_staging_storage"
            f" {constraints.max_staging_storage_bytes} 字节"
            f"（超 {(storage_total_bytes - constraints.max_staging_storage_bytes)} 字节）"
        )

    if storage_target is not None and storage_target.max_total is not None:
        limit = parse_storage_bytes(storage_target.max_total)
        if storage_total_bytes > limit:
            conflicts.append(
                f"估算总存储 {storage_total_bytes} 字节超过存储目标 "
                f"{storage_target.max_total}（={limit} 字节，超 "
                f"{storage_total_bytes - limit} 字节）"
            )

    if constraints.max_generation_time_ms is not None:
        if s.generation_throughput_rows_per_sec <= 0:
            conflicts.append(
                f"求解器吞吐模型非法：generation_throughput_rows_per_sec="
                f"{s.generation_throughput_rows_per_sec}（必须为正）"
            )
        else:
            est_ms = total_rows / s.generation_throughput_rows_per_sec * 1000.0
            if est_ms > constraints.max_generation_time_ms:
                conflicts.append(
                    f"按吞吐模型 {s.generation_throughput_rows_per_sec} 行/秒估算生成"
                    f"耗时 {est_ms:.0f} 毫秒（总行数 {total_rows}），超过约束 "
                    f"max_generation_time {constraints.max_generation_time_ms} 毫秒"
                )

    if constraints.max_database_connections is not None \
            and s.planned_loader_connections > constraints.max_database_connections:
        conflicts.append(
            f"规划期装载并发 {s.planned_loader_connections} 连接超过约束 "
            f"max_database_connections {constraints.max_database_connections}"
        )

    return conflicts


def solve(
    rows_by_table: dict[str, int],
    storage_total_bytes: int,
    constraints: ResourceConstraints,
    storage_target: StorageTarget | None = None,
    settings: SolverSettings | None = None,
) -> SolveResult:
    """求解入口：全部约束满足返回结果；否则抛 PlanningConflict（逐条冲突）。

    输入 rows_by_table 只读——不可满足时直接失败，不做静默缩减（A1 分轨）。
    """
    conflicts = check_constraints(
        rows_by_table, storage_total_bytes, constraints, storage_target, settings
    )
    s = settings or SolverSettings()
    total_rows = sum(rows_by_table.values())
    est_ms = (total_rows / s.generation_throughput_rows_per_sec * 1000.0
              if s.generation_throughput_rows_per_sec > 0 else 0.0)
    if conflicts:
        raise PlanningConflict(conflicts)
    return SolveResult(
        total_rows=total_rows,
        estimated_generation_time_ms=est_ms,
        estimated_storage_bytes=storage_total_bytes,
        throughput_rows_per_sec=s.generation_throughput_rows_per_sec,
    )
