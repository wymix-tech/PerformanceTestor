"""T6.4 业务不变量验证（实施方案 §9.2 T6.4；总方案 §18.4 条件 5 / §5.3）。

- **仅接受 status=CONFIRMED 的 BusinessInvariant**（AI 推断不作为硬规则）：
  INFERRED/UNKNOWN 一律跳过（WARN 记录，不执行、不伪装通过）；
- 结构化 predicate 编译为验证 SQL（白名单校验延续 R2 机制）：
  - ``relation_exists``：when{table,column} require{table,column} →
    全量孤儿核对（孤儿=0 通过）；
  - ``not_null``：when{table,column} → 全量 NULL 计数（0 通过）；
- 已确认但谓词缺失/类型不可编译 → **显式 FAIL**（已确认不变量无法执行
  验证不静默放行，A3）。
"""

from __future__ import annotations

from typing import Any

from ptde.models.base import EvidenceStatus
from ptde.models.dataset import LayerKind, LayerResult, ValidationStatus
from ptde.models.entity import BusinessInvariant
from ptde.validator import (
    layer_result,
    make_check,
    readonly_scalar,
    validate_identifier,
)

__all__ = ["validate_invariants"]


def _compile_relation_exists(conn, inv: BusinessInvariant) -> tuple[ValidationStatus, str]:
    """relation_exists 编译执行：全量孤儿核对。"""
    when = inv.predicate.when or {}
    require = inv.predicate.require or {}
    missing = [
        k for k, d in (
            ("when.table", when.get("table")), ("when.column", when.get("column")),
            ("require.table", require.get("table")),
            ("require.column", require.get("column")),
        ) if not d
    ]
    if missing:
        return ValidationStatus.FAIL, (
            f"predicate 编译失败（缺字段 {missing}——已确认不变量无法执行，显式失败）"
        )
    child_table, child_col = str(when["table"]), str(when["column"])
    parent_table, parent_col = str(require["table"]), str(require["column"])
    try:
        ct, cc, pt, pc = (
            validate_identifier(child_table), validate_identifier(child_col),
            validate_identifier(parent_table), validate_identifier(parent_col),
        )
    except Exception as exc:  # noqa: BLE001 — 标识符白名单拒绝即编译失败
        return ValidationStatus.FAIL, f"predicate 编译失败（标识符白名单拒绝）: {exc}"
    sql = (
        f"SELECT COUNT(*) FROM (SELECT {cc} FROM {ct} WHERE {cc} IS NOT NULL) s "
        f"LEFT JOIN {pt} p ON s.{cc} = p.{pc} WHERE p.{pc} IS NULL"
    )
    orphans = int(readonly_scalar(conn, sql) or 0)
    if orphans == 0:
        return ValidationStatus.PASS, (
            f"全量孤儿核对=0（{ct}.{cc} -> {pt}.{pc}，非抽样强完整性证据）"
        )
    return ValidationStatus.FAIL, (
        f"发现 {orphans} 条孤儿（{ct}.{cc} -> {pt}.{pc}，已确认不变量违规）"
    )


def _compile_not_null(conn, inv: BusinessInvariant) -> tuple[ValidationStatus, str]:
    """not_null 编译执行：全量 NULL 计数。"""
    when = inv.predicate.when or {}
    table, column = str(when.get("table") or ""), str(when.get("column") or "")
    if not table or not column:
        return ValidationStatus.FAIL, (
            "predicate 编译失败（not_null 缺 when.table/when.column——显式失败）"
        )
    try:
        t, c = validate_identifier(table), validate_identifier(column)
    except Exception as exc:  # noqa: BLE001
        return ValidationStatus.FAIL, f"predicate 编译失败（标识符白名单拒绝）: {exc}"
    nulls = int(readonly_scalar(
        conn, f"SELECT COUNT(*) FROM {t} WHERE {c} IS NULL") or 0)
    if nulls == 0:
        return ValidationStatus.PASS, f"全量 NULL 核对=0（{t}.{c}，非抽样）"
    return ValidationStatus.FAIL, (
        f"发现 {nulls} 条 NULL（{t}.{c}，已确认不变量违规）"
    )


_COMPILERS = {"relation_exists": _compile_relation_exists, "not_null": _compile_not_null}


def validate_invariants(
    conn,
    invariants: list[BusinessInvariant],
) -> LayerResult:
    """业务不变量验证（仅 CONFIRMED 执行；按 id 排序，确定性输出）。"""
    checks: list = []
    metrics: dict[str, Any] = {
        "total": 0, "confirmed": 0, "executed": 0, "skipped": 0,
    }
    for inv in sorted(invariants, key=lambda i: i.id):
        metrics["total"] += 1
        if inv.status is not EvidenceStatus.CONFIRMED:
            metrics["skipped"] += 1
            checks.append(make_check(
                f"invariant:{inv.id}", ValidationStatus.WARN,
                detail=f"status={inv.status.value}（AI 推断不作为硬规则）——"
                "跳过执行，不参与 READY 硬门禁",
            ))
            continue
        metrics["confirmed"] += 1
        if inv.predicate is None:
            checks.append(make_check(
                f"invariant:{inv.id}", ValidationStatus.FAIL,
                detail="已确认不变量缺少结构化谓词（无法编译执行——显式失败，"
                "不伪装通过；不可结构化表达的规则应登记为 UNKNOWN 待人工审核）",
            ))
            continue
        compiler = _COMPILERS.get((inv.predicate.type or "").lower())
        if compiler is None:
            checks.append(make_check(
                f"invariant:{inv.id}", ValidationStatus.FAIL,
                detail=f"谓词类型 {inv.predicate.type!r} 不可编译为验证 SQL"
                "（已确认不变量无法执行——显式失败，不伪装通过）",
            ))
            continue
        metrics["executed"] += 1
        status, detail = compiler(conn, inv)
        checks.append(make_check(f"invariant:{inv.id}", status, detail=detail))
    return layer_result(LayerKind.BUSINESS, checks, metrics=metrics)
