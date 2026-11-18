"""T6.1 数量验证（实施方案 §9.2 T6.1；总方案 §18.4 条件 1/2）。

- 目标行数 vs 实际行数：逐表 ``SELECT COUNT(*)`` 全表精确核对（非抽样，
  强完整性证据；QW3 verify_min 行数核对收编升级）；
- 分片之和一致：分片装载行数之和（staging/manifest 汇总）与实际行数
  显式核对，不一致即 FAIL（数量复核锚点，§8.6 门禁 1）；
- 精确目标与统计容差分别处理（实施方案 T6.1 原文）：
  - 精确目标（source ∈ {explicit, derived}）必须精确相等；
  - 统计目标（source ∈ {production, estimated}）允许相对容差——
    容差默认 0（**无通用固定值**，须由调用方按 Pilot Run 校准显式配置，
    与 R5 LoadBudget 口径同源）；容差内偏差记 WARN（可降级项）。
"""

from __future__ import annotations

from pydantic import Field

from ptde.models.dataset import LayerKind, LayerResult, ValidationStatus
from ptde.models.spec import RowSource
from ptde.validator import (
    layer_result,
    make_check,
    readonly_scalar,
    validate_identifier,
)
from ptde_common.model_base import VersionedModel

__all__ = [
    "APPROX_SOURCES",
    "CountTolerance",
    "EXACT_SOURCES",
    "validate_counts",
]

# 精确目标来源（必须精确相等）与统计目标来源（允许容差）
EXACT_SOURCES: frozenset[str] = frozenset({
    RowSource.EXPLICIT.value,
    RowSource.DERIVED.value,
})
APPROX_SOURCES: frozenset[str] = frozenset({
    RowSource.PRODUCTION.value,
    RowSource.ESTIMATED.value,
})


class CountTolerance(VersionedModel):
    """数量验证容差（仅统计目标可用；无通用固定值，须显式配置）。

    relative：相对容差（如 0.02 = ±2%）；0 = 逐行精确。
    判定：精确相等 → PASS；容差内偏差 → WARN（可降级项，随报告留痕）；
    超出 → FAIL。
    """

    relative: float = Field(default=0.0, ge=0.0, le=1.0)

    def judge(self, expected: int, actual: int) -> ValidationStatus:
        """精确/容差判定：精确目标恒判 PASS/FAIL；统计目标按容差分级。"""
        if expected == actual:
            return ValidationStatus.PASS
        if self.relative <= 0.0:
            return ValidationStatus.FAIL
        ratio = abs(actual - expected) / abs(expected)
        if ratio <= self.relative:
            return ValidationStatus.WARN
        return ValidationStatus.FAIL


def validate_counts(
    conn,
    expected: dict[str, int],
    *,
    sources: dict[str, str] | None = None,
    shard_loaded: dict[str, int] | None = None,
    tolerance: CountTolerance | None = None,
) -> LayerResult:
    """逐表数量验证（表名排序，确定性输出）。

    - ``expected``：表 → 目标行数（精确目标）；
    - ``sources``：表 → RowSource 值（缺省全部按精确目标处理）；
    - ``shard_loaded``：表 → 分片装载行数之和（staging totals / manifest
      shard_summaries 汇总）；提供即与实际行数显式核对（分片之和一致）；
    - ``tolerance``：统计容差（默认 0 = 全部精确核对）。
    """
    tol = tolerance or CountTolerance()
    srcs = sources or {}
    shard = shard_loaded or {}
    checks = []
    metrics: dict = {
        "tables_checked": 0,
        "rows_expected": 0,
        "rows_actual": 0,
        "exact_tables": 0,
        "tolerated_tables": 0,
    }
    evidence: list[str] = []
    for table in sorted(expected):
        q = validate_identifier(table)
        actual_raw = readonly_scalar(conn, f"SELECT COUNT(*) FROM {q}")
        if actual_raw is None:
            checks.append(make_check(
                f"count:{table}", ValidationStatus.FAIL,
                detail="实际行数不可得（COUNT(*) 无返回——显式失败，不伪装通过）",
            ))
            continue
        actual = int(actual_raw)
        target = int(expected[table])
        source = srcs.get(table)
        is_exact = source is None or source in EXACT_SOURCES
        if is_exact:
            status = ValidationStatus.PASS if actual == target else ValidationStatus.FAIL
            if status is ValidationStatus.PASS:
                metrics["exact_tables"] += 1
                evidence.append(f"{table}: COUNT(*) 全表精确核对（非抽样）")
        else:
            if source not in APPROX_SOURCES:
                checks.append(make_check(
                    f"count:{table}", ValidationStatus.FAIL,
                    detail=f"行数来源 {source!r} 不在已知来源集合（精确="
                    f"{sorted(EXACT_SOURCES)}，统计={sorted(APPROX_SOURCES)}）",
                ))
                continue
            status = tol.judge(target, actual)
            if status is ValidationStatus.WARN:
                metrics["tolerated_tables"] += 1
        metrics["tables_checked"] += 1
        metrics["rows_expected"] += target
        metrics["rows_actual"] += actual
        delta = actual - target
        detail = (
            f"目标 {target}，实际 {actual}，偏差 {delta:+d}"
            + ("" if is_exact else f"（统计目标，容差 relative={tol.relative}）")
        )
        checks.append(make_check(f"count:{table}", status, detail=detail))

        # 分片之和一致（提供即核对；数量复核锚点）
        if table in shard:
            shard_sum = int(shard[table])
            if shard_sum != actual:
                checks.append(make_check(
                    f"shard_sum:{table}", ValidationStatus.FAIL,
                    detail=f"分片装载行数之和 {shard_sum} ≠ 实际行数 {actual}"
                    "（数量复核锚点不一致，显式失败）",
                ))
            else:
                checks.append(make_check(
                    f"shard_sum:{table}", ValidationStatus.PASS,
                    detail=f"分片之和 {shard_sum} = 实际行数（全量非抽样核对）",
                ))
    return layer_result(
        LayerKind.COUNT, checks, metrics=metrics, evidence=evidence,
        tolerance={"relative": tol.relative},
    )
