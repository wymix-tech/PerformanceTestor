"""五层数据验证器（R6 T6.1~T6.5，实施方案 §9；总方案 §17/§18.4）。

验证器为只读组件（仅 SELECT/统计语句，实施方案 §9.6）——全部查询经
``readonly_scalar`` / ``readonly_rows`` 发送前语句白名单校验（延续 R2 机制：
``ptde.profiler.collector.assert_readonly_sql``，白名单外直接拒绝不入库）；
表/列标识符拼接前必须过 ``validate_identifier``（收编口径，防注入）。

各层输出统一 ``LayerResult``（层内逐项 ``CheckResult``，聚合规则：
任一 FAIL → FAIL；否则任一 WARN → WARN；否则 PASS）。
可降级项（统计容差内偏差）→ WARN 记录，是否放行由调用方配置
（实施方案 §9.4：READY 判定门禁）。

legacy 收编（实施方案 §9.3）：
- QW3 ``datagen/verify_min.py`` 三项检查分别收编为 T6.1（行数核对，升级为
  全量非抽样）、T6.2（孤儿核对，升级为全量）、T6.3（枚举合法率 →
  类别配额/合法率核对）；
- ``datagen_service`` selfCheck 收编为 T6.5 子项（端点抽检）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ptde.adapters.mysql.statistics import (
    StatisticsCollectionError,
    validate_identifier,
)
from ptde.models.dataset import (
    CheckResult,
    LayerKind,
    LayerResult,
    ValidationStatus,
)
from ptde.profiler.collector import assert_readonly_sql

__all__ = [
    "StatisticsCollectionError",
    "ValidationStatus",
    "aggregate",
    "layer_result",
    "make_check",
    "readonly_rows",
    "readonly_scalar",
    "validate_identifier",
]


def readonly_scalar(conn, sql: str, params: Sequence[Any] | None = None) -> Any:
    """单值只读查询：语句白名单校验（R2 机制延续）后执行，返回第一行第一列。

    无行返回 None；游标用完即关（与 legacy verify_min 同构）。
    """
    sql = assert_readonly_sql(sql)
    cur = conn.cursor()
    try:
        cur.execute(sql, list(params or []))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


def readonly_rows(conn, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
    """多行只读查询：语句白名单校验（R2 机制延续）后执行。"""
    sql = assert_readonly_sql(sql)
    cur = conn.cursor()
    try:
        cur.execute(sql, list(params or []))
        return [tuple(r) for r in cur.fetchall()]
    finally:
        cur.close()


def make_check(
    name: str,
    status: ValidationStatus = ValidationStatus.PASS,
    detail: str | None = None,
) -> CheckResult:
    """构造单条检查结果（统一入口，便于断言与留痕）。"""
    return CheckResult(name=name, status=status, detail=detail)


def aggregate(checks: list[CheckResult]) -> ValidationStatus:
    """层内聚合：任一 FAIL → FAIL；否则任一 WARN → WARN；否则 PASS。"""
    statuses = {c.status for c in checks}
    if ValidationStatus.FAIL in statuses:
        return ValidationStatus.FAIL
    if ValidationStatus.WARN in statuses:
        return ValidationStatus.WARN
    return ValidationStatus.PASS


def layer_result(
    kind: LayerKind,
    checks: list[CheckResult],
    *,
    metrics: dict | None = None,
    evidence: list[str] | None = None,
    tolerance: dict | None = None,
) -> LayerResult:
    """按聚合规则组装 LayerResult（metrics/evidence 供报告审计）。"""
    return LayerResult(
        layer=kind,
        status=aggregate(checks),
        metrics=metrics or {},
        checks=checks,
        tolerance=tolerance,
        evidence=evidence or [],
    )
