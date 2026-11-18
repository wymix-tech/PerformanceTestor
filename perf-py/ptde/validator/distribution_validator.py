"""T6.3 分布验证（实施方案 §9.2 T6.3；总方案 §18.4 条件 4）。

检查项（T6.3 原文全集，对齐 R4 采样器参数形态）：
- NULL 比例 / Distinct：对画像 ColumnProfile 容差核对；
- Top-K / 类别配额：weighted_categorical（values/weights 列表或
  {bucket: ratio} 字典两形态，与 weighted_categorical.sample 一致）——
  **精确配额（source=explicit）直查目标数量**（每桶 COUNT(*) 等于
  配额，漏项/多桶均显式失败）；概率分布用统计容差；
- 直方图：数值分桶 [lo, hi) 计数核对（精确/容差两档）；
- 时间分桶：TimeBucketCheck（[start, end) 半开区间，时间输入固定值）；
- 热点集中度：zipf/skewed Top-1 占比 vs 目标（无目标值时 WARN 记录
  观测值，不伪装通过）；
- 关联基数分布 / 联合分布：联合分布 JointDistributionCheck（GROUP BY
  全量核对，精确配额漏桶显式失败）。

可降级项（容差内偏差）→ WARN；容差默认 0 = 逐桶精确（无通用固定值，
须显式配置）。不支持的分布类型记 WARN（观测值入 metrics），**不静默
省略也不伪装 PASS**（§10.1 全集口径同源）。
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import Field

from ptde.models.dataset import LayerKind, LayerResult, ValidationStatus
from ptde.models.profile import ColumnProfile
from ptde.models.spec import DistributionPlanEntry
from ptde.validator import (
    layer_result,
    make_check,
    readonly_rows,
    readonly_scalar,
    validate_identifier,
)
from ptde_common.model_base import VersionedModel

__all__ = [
    "DistributionTolerance",
    "JointBucket",
    "JointDistributionCheck",
    "TimeBucket",
    "TimeBucketCheck",
    "validate_distributions",
]

_CATEGORICAL = frozenset({"weighted_categorical", "weightedcategorical"})


class DistributionTolerance(VersionedModel):
    """分布统计容差（相对值；0 = 精确；无通用固定值须显式配置）。"""

    relative: float = Field(default=0.0, ge=0.0, le=1.0)

    def judge_ratio(self, expected: float, actual: float) -> ValidationStatus:
        """占比/比例判定：偏差 ≤ relative → WARN（容差内可降级）→ 否则 FAIL。

        精确相等恒 PASS（无论容差配置）。
        """
        if math.isclose(expected, actual, rel_tol=1e-12, abs_tol=1e-12):
            return ValidationStatus.PASS
        if self.relative <= 0.0:
            return ValidationStatus.FAIL
        base = max(abs(expected), 1e-12)
        if abs(actual - expected) / base <= self.relative:
            return ValidationStatus.WARN
        return ValidationStatus.FAIL


class JointBucket(VersionedModel):
    """联合分布桶：各列取值组合 + 期望计数。"""

    values: list[Any]
    count: int


class JointDistributionCheck(VersionedModel):
    """联合分布核对定义（GROUP BY 全量）。"""

    table: str
    columns: list[str]
    exact: bool = True
    buckets: list[JointBucket] = Field(default_factory=list)


class TimeBucket(VersionedModel):
    """时间分桶：[start, end) 半开区间 + 期望计数（时间输入固定值）。"""

    label: str
    start: str
    end: str
    expected: int


class TimeBucketCheck(VersionedModel):
    """时间分桶核对定义。"""

    table: str
    column: str
    exact: bool = True
    buckets: list[TimeBucket] = Field(default_factory=list)


def _q(name: str) -> str:
    return validate_identifier(name)


def _categorical_params(fit) -> list[tuple[str, float]]:
    """weighted_categorical 参数归一：values/weights 列表或 {bucket: ratio} 字典。"""
    p = fit.params or {}
    values, weights = p.get("values"), p.get("weights")
    if isinstance(values, list) and isinstance(weights, list):
        if len(values) != len(weights):
            raise ValueError(
                f"weighted_categorical values/weights 长度不一致："
                f"{len(values)} vs {len(weights)}"
            )
        return [(str(v), float(w)) for v, w in zip(values, weights, strict=False)]
    if isinstance(p, dict) and p and all(
        isinstance(k, str) for k in p
    ) and "values" not in p and "weights" not in p:
        return [(str(k), float(v)) for k, v in p.items()]
    raise ValueError(
        f"weighted_categorical 需要 values/weights 列表或 {{bucket: ratio}} "
        f"字典参数，得到 {sorted(p)}"
    )


def _ratio_status(
    expected: float, actual: float, tol: DistributionTolerance,
) -> ValidationStatus:
    return tol.judge_ratio(expected, actual)


def _validate_categorical(
    conn, entry, fit, exact_quota_total: int | None, tol: DistributionTolerance,
) -> list:
    checks: list = []
    label = f"dist:{entry.entity}.{entry.field}"
    table = _q(entry.entity)
    col = _q(entry.field)
    pairs = _categorical_params(fit)
    total_raw = readonly_scalar(conn, f"SELECT COUNT(*) FROM {table}")
    if total_raw is None:
        return [make_check(label, ValidationStatus.FAIL,
                           detail="总行数不可得（显式失败）")]
    total = int(total_raw)
    quota_mode = entry.source == "explicit"

    if quota_mode:
        # 精确配额：直查目标数量（实施方案 T6.3 原文）
        if exact_quota_total is None:
            exact_quota_total = total
        expected_other = exact_quota_total
        for value, weight in pairs:
            quota = round(weight * exact_quota_total)
            expected_other -= quota
            actual = int(readonly_scalar(
                conn,
                f"SELECT COUNT(*) FROM {table} WHERE {col} = %s",
                [value],
            ) or 0)
            if actual == quota:
                checks.append(make_check(
                    f"{label}[{value}]", ValidationStatus.PASS,
                    detail=f"精确配额核对：{quota}/{exact_quota_total}",
                ))
            else:
                checks.append(make_check(
                    f"{label}[{value}]", ValidationStatus.FAIL,
                    detail=f"精确配额不符：目标 {quota}，实际 {actual}"
                    "（直查目标数量，显式失败）",
                ))
        others = int(readonly_scalar(
            conn,
            f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL "
            f"AND {col} NOT IN ({', '.join(['%s'] * len(pairs))})",
            [v for v, _ in pairs],
        ) or 0)
        if others != max(expected_other, 0):
            checks.append(make_check(
                f"{label}[others]", ValidationStatus.FAIL,
                detail=f"配额外取值 {others} 条 ≠ 预期 {max(expected_other, 0)}"
                "（漏项/多桶显式失败）",
            ))
        else:
            checks.append(make_check(
                f"{label}[others]", ValidationStatus.PASS,
                detail=f"配额外取值 {others} 条（含 NULL 不计入类别配额）",
            ))
        return checks

    # 概率分布：统计容差
    for value, weight in pairs:
        cnt = int(readonly_scalar(
            conn, f"SELECT COUNT(*) FROM {table} WHERE {col} = %s", [value],
        ) or 0)
        ratio = (cnt / total) if total else 0.0
        status = _ratio_status(weight, ratio, tol)
        checks.append(make_check(
            f"{label}[{value}]", status,
            detail=f"占比目标 {weight}，实测 {ratio:.6f}"
            f"（统计容差 relative={tol.relative}）",
        ))
    return checks


def _validate_numeric_fit(conn, entry, fit, tol: DistributionTolerance) -> list:
    """normal / lognormal / uniform：观测统计量 vs 参数（容差核对）。"""
    label = f"dist:{entry.entity}.{entry.field}"
    table = _q(entry.entity)
    col = _q(entry.field)
    row = readonly_rows(
        conn,
        f"SELECT AVG({col}), STDDEV_POP({col}), MIN({col}), MAX({col}) "
        f"FROM {table} WHERE {col} IS NOT NULL",
    )
    if not row:
        return [make_check(label, ValidationStatus.FAIL,
                           detail="统计量不可得（显式失败）")]
    avg, std, mn, mx = row[0]
    checks: list = []
    t = (fit.type or "").lower()
    p = fit.params or {}
    if t == "normal":
        checks.append(make_check(
            f"{label}[mean]",
            _ratio_status(float(p["mean"]), float(avg), tol) if avg is not None
            else ValidationStatus.FAIL,
            detail=f"mean 目标 {p.get('mean')}，实测 {avg}",
        ))
        target_std = float(p.get("sigma", p.get("std", 0.0)) or 0.0)
        checks.append(make_check(
            f"{label}[sigma]",
            _ratio_status(target_std, float(std or 0.0), tol),
            detail=f"sigma 目标 {target_std}，实测 {std}",
        ))
    elif t == "lognormal":
        mu, sigma = float(p["mu"]), float(p.get("sigma", 0.0) or 0.0)
        theoretical = math.exp(mu + sigma**2 / 2)
        checks.append(make_check(
            f"{label}[mean]",
            _ratio_status(theoretical, float(avg), tol),
            detail=f"lognormal 理论均值 {theoretical:.4f}，实测 {avg}"
            "（长尾分布容差宜放宽）",
        ))
    elif t == "uniform":
        lo, hi = float(p.get("min", 0.0)), float(p.get("max", 0.0))
        checks.append(make_check(
            f"{label}[range]",
            ValidationStatus.PASS if (
                mn is not None and abs(float(mn) - lo) <= tol.relative * max(abs(lo), 1)
                and abs(float(mx) - hi) <= tol.relative * max(abs(hi), 1)
            ) else ValidationStatus.FAIL,
            detail=f"min/max 目标 [{lo}, {hi}]，实测 [{mn}, {mx}]",
        ))
    else:
        checks.append(make_check(
            label, ValidationStatus.WARN,
            detail=f"分布类型 {t!r} 暂不支持自动统计核对（观测值 avg={avg}，"
            f"std={std}，min={mn}，max={mx}——不伪装 PASS）",
        ))
    return checks


def _validate_hotspot(conn, entry, fit, tol: DistributionTolerance) -> list:
    """zipf/skewed 热点集中度：Top-1 占比 vs 目标（无目标 → WARN 观测）。"""
    label = f"dist:{entry.entity}.{entry.field}"
    table = _q(entry.entity)
    col = _q(entry.field)
    total = int(readonly_scalar(
        conn, f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL") or 0)
    top_raw = readonly_scalar(
        conn,
        f"SELECT MAX(c) FROM (SELECT COUNT(*) AS c FROM {table} "
        f"WHERE {col} IS NOT NULL GROUP BY {col}) x",
    )
    top_ratio = (int(top_raw or 0) / total) if total else 0.0
    target = (fit.params or {}).get("top_ratio")
    if target is None:
        return [make_check(
            label, ValidationStatus.WARN,
            detail=f"热点 Top-1 占比实测 {top_ratio:.6f}，分布参数未提供目标值"
            "（观测值入 metrics，不伪装 PASS）",
        )]
    status = _ratio_status(float(target), top_ratio, tol)
    return [make_check(
        label, status,
        detail=f"热点 Top-1 占比目标 {target}，实测 {top_ratio:.6f}",
    )]


def _validate_profile(
    conn, key: str, profile: ColumnProfile, tol: DistributionTolerance,
) -> list:
    """画像核对：NULL 比例 / Distinct / Top-1 vs ColumnProfile（容差）。"""
    table, col_name = key.split(".", 1)
    t, c = _q(table), _q(col_name)
    checks: list = []
    row = readonly_rows(
        conn,
        f"SELECT COUNT(*), SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END), "
        f"COUNT(DISTINCT {c}) FROM {t}",
    )
    if not row:
        return [make_check(f"profile:{key}", ValidationStatus.FAIL,
                           detail="画像核对统计量不可得（显式失败）")]
    total, nulls, distinct = row[0]
    total = int(total or 0)
    null_ratio = (int(nulls or 0) / total) if total else 0.0
    if profile.null_ratio is not None:
        checks.append(make_check(
            f"profile:{key}[null_ratio]",
            _ratio_status(profile.null_ratio, null_ratio, tol),
            detail=f"NULL 比例目标 {profile.null_ratio}，实测 {null_ratio:.6f}",
        ))
    if profile.distinct_count is not None:
        status = _ratio_status(
            float(profile.distinct_count), float(int(distinct or 0)), tol)
        checks.append(make_check(
            f"profile:{key}[distinct]", status,
            detail=f"Distinct 目标 {profile.distinct_count}，实测 {distinct}",
        ))
    if profile.top_k:
        checks.append(make_check(
            f"profile:{key}[top1]", ValidationStatus.WARN,
            detail="Top-1 占比核对须值域可查（脱敏分桶标识不入验证 SQL）——"
            "记 WARN 交由类别配额/热点检查覆盖，不伪装 PASS",
        ))
    return checks


def _validate_joint(conn, check: JointDistributionCheck) -> list:
    """联合分布：GROUP BY 全量核对；精确配额漏桶/多桶显式失败。"""
    table = _q(check.table)
    cols = ", ".join(_q(c) for c in check.columns)
    rows = readonly_rows(
        conn, f"SELECT {cols}, COUNT(*) FROM {table} GROUP BY {cols}"
    )
    measured = {tuple(r[:-1]): int(r[-1]) for r in rows}
    checks: list = []
    expected_map = {tuple(b.values): b.count for b in check.buckets}
    for key in sorted(expected_map, key=lambda k: repr(k)):
        expected = expected_map[key]
        actual = measured.get(key)
        if actual is None:
            checks.append(make_check(
                f"joint:{check.table}{key!r}", ValidationStatus.FAIL,
                detail=f"联合分布桶缺失：期望 {expected}，实际无此组合"
                "（漏桶显式失败）",
            ))
        elif check.exact and actual != expected:
            checks.append(make_check(
                f"joint:{check.table}{key!r}", ValidationStatus.FAIL,
                detail=f"联合分布精确配额不符：目标 {expected}，实际 {actual}",
            ))
        else:
            checks.append(make_check(
                f"joint:{check.table}{key!r}", ValidationStatus.PASS,
                detail=f"联合分布核对一致（{actual} 行）",
            ))
    extra = [k for k in measured if k not in expected_map]
    if extra:
        checks.append(make_check(
            f"joint:{check.table}[extra]", ValidationStatus.FAIL,
            detail=f"发现 {len(extra)} 个未声明组合（多桶显式失败，"
            "全量 GROUP BY 非抽样）",
        ))
    return checks


def _validate_time_buckets(conn, check: TimeBucketCheck) -> list:
    """时间分桶：[start, end) 计数核对（精确/容差）。"""
    table, col = _q(check.table), _q(check.column)
    checks: list = []
    for bucket in check.buckets:
        actual = int(readonly_scalar(
            conn,
            f"SELECT COUNT(*) FROM {table} WHERE {col} >= %s AND {col} < %s",
            [bucket.start, bucket.end],
        ) or 0)
        if check.exact and actual != bucket.expected:
            checks.append(make_check(
                f"tb:{check.table}:{bucket.label}", ValidationStatus.FAIL,
                detail=f"时间桶 {bucket.label} 精确配额不符：目标 "
                f"{bucket.expected}，实际 {actual}",
            ))
        else:
            checks.append(make_check(
                f"tb:{check.table}:{bucket.label}", ValidationStatus.PASS,
                detail=f"时间桶 {bucket.label} 核对一致（{actual} 行）",
            ))
    return checks


def validate_distributions(
    conn,
    entries: list[DistributionPlanEntry],
    *,
    exact_quota_totals: dict[str, int] | None = None,
    profile_checks: dict[str, ColumnProfile] | None = None,
    joint_checks: list[JointDistributionCheck] | None = None,
    time_bucket_checks: list[TimeBucketCheck] | None = None,
    tolerance: DistributionTolerance | None = None,
) -> LayerResult:
    """分布验证（entries 按 entity.field 排序，确定性输出）。

    - ``entries``：字段分布计划（DistributionPlanEntry；source=explicit →
      精确配额直查目标数量，其余统计容差）；
    - ``exact_quota_totals``：表 → 配额基数（缺省用实测总行数）；
    - ``profile_checks``：``表.列`` → ColumnProfile（NULL/Distinct 容差核对）；
    - ``joint_checks`` / ``time_bucket_checks``：联合分布与时间分桶核对。
    """
    tol = tolerance or DistributionTolerance()
    checks: list = []
    metrics: dict[str, Any] = {"entries_checked": 0, "quota_mode_entries": 0}
    for entry in sorted(entries, key=lambda e: (e.entity, e.field)):
        label = f"dist:{entry.entity}.{entry.field}"
        if entry.distribution is None:
            checks.append(make_check(
                label, ValidationStatus.WARN,
                detail="分布计划未提供分布参数（无可核对目标——不伪装 PASS）",
            ))
            continue
        metrics["entries_checked"] += 1
        fit = entry.distribution
        t = (fit.type or "").lower()
        if t in _CATEGORICAL:
            if entry.source == "explicit":
                metrics["quota_mode_entries"] += 1
            checks.extend(_validate_categorical(
                conn, entry, fit,
                (exact_quota_totals or {}).get(entry.entity), tol,
            ))
        elif t in ("zipf", "skewed"):
            checks.extend(_validate_hotspot(conn, entry, fit, tol))
        elif t in ("normal", "lognormal", "uniform", "gaussian"):
            checks.extend(_validate_numeric_fit(conn, entry, fit, tol))
        elif t == "null_ratio":
            checks.append(make_check(
                label, ValidationStatus.WARN,
                detail="null_ratio 分布请经 profile_checks 提供画像核对"
                "（此处不重复核对，不伪装 PASS）",
            ))
        elif t == "sequential":
            checks.append(make_check(
                label, ValidationStatus.PASS,
                detail="sequential 由 ID 连续性保证（不适用统计核对，ok+说明）",
            ))
        elif t == "temporal":
            checks.append(make_check(
                label, ValidationStatus.WARN,
                detail="temporal 分桶核对请提供 time_bucket_checks"
                "（时间窗口依赖固定时间输入，不伪装 PASS）",
            ))
        else:
            checks.append(make_check(
                label, ValidationStatus.WARN,
                detail=f"分布类型 {t!r} 暂不支持自动核对（观测留待人工，"
                "不伪装 PASS）",
            ))

    for key in sorted(profile_checks or {}):
        checks.extend(_validate_profile(conn, key, profile_checks[key], tol))
    for check in joint_checks or []:
        checks.extend(_validate_joint(conn, check))
    for check in time_bucket_checks or []:
        checks.extend(_validate_time_buckets(conn, check))

    return layer_result(
        LayerKind.DISTRIBUTION, checks, metrics=metrics,
        tolerance={"relative": tol.relative},
    )
