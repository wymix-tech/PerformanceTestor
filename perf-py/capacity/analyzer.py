"""容量分析器：移植自 CapacityAnalyzer.java（含 CapacityResult 结构）。

分段线性插值求目标 RT 阈值下的最大并发/QPS（历史口径，语义不变）+
科学化模型补充（拟合/R²/拐点/置信区间，仅展示解释，不参与判定）。
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from capacity.curve_fitter import (
    fit_quadratic,
    knee_concurrency,
    p95_interval,
    qps_interval,
)
from engine.result import StagePoint  # 复用对齐 Java 契约的分档采样点

ERROR_CAP = 0.05  # 错误率截断阈值（超过视为过载档，不参与插值）
FITTED_POINTS = 24  # 拟合曲线重采样点数


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class CurvePoint(CamelModel):
    concurrency: int
    qps: float
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    error_rate: float = 0.0
    total_requests: int = 0


class FitModel(CamelModel):
    type: str = "quadratic"
    a: float = 0.0
    b: float = 0.0
    k: float = 0.0
    r2: float = 0.0
    vertex_concurrency: float | None = None
    vertex_qps: float | None = None
    points: int = 0
    formula: str = ""


class FittedPoint(CamelModel):
    concurrency: float
    qps: float


class ConfidenceInterval(CamelModel):
    concurrency: int
    qps: float
    qps_samples: float = 0.0
    qps_lower: float = 0.0
    qps_upper: float = 0.0
    p95_ms: float = 0.0
    p95_lower: float = 0.0
    p95_upper: float = 0.0


class CapacityResult(CamelModel):
    """对齐 CapacityResult.java（advices 由 ScaleAdviceEngine 产生，本轮不移植）。"""

    max_concurrency: int = 0
    max_qps: int = 0
    target_p95_ms: float = 0.0
    extrapolated: bool = False
    curve: list[CurvePoint] = []
    r2: float = 0.0
    fitted_curve: list[FittedPoint] = []
    confidence_interval: list[ConfidenceInterval] = []
    rt_knee_concurrency: float = 0.0
    model: FitModel | None = None

    def to_java_json(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


def _round2(v: float) -> float:
    return math.floor(v * 100 + 0.5) / 100.0


def _round4(v: float) -> float:
    return math.floor(v * 10000 + 0.5) / 10000.0


def _effective_end(curve: list[CurvePoint]) -> int:
    """有效档位上界（首个错误率超限档位的下标，全部有效时为 len）。"""
    for i, p in enumerate(curve):
        if p.error_rate > ERROR_CAP:
            return 1 if i == 0 else i
    return len(curve)


def _interpolate(points: list[StagePoint], target_p95_ms: float) -> CapacityResult:
    result = CapacityResult(target_p95_ms=target_p95_ms)
    if not points:
        return result

    sorted_points = sorted(points, key=lambda p: p.concurrency)
    curve = [
        CurvePoint(
            concurrency=p.concurrency,
            qps=p.qps,
            p50_ms=p.p50_ms,
            p95_ms=p.p95_ms,
            p99_ms=p.p99_ms,
            error_rate=p.error_rate,
            total_requests=p.total_requests,
        )
        for p in sorted_points
        if p.concurrency > 0
    ]
    result.curve = curve
    if not curve:
        return result

    end = _effective_end(curve)
    threshold = target_p95_ms

    # 首档已超阈值：容量即为首档
    if curve[0].p95_ms > threshold:
        result.max_concurrency = curve[0].concurrency
        result.max_qps = int(curve[0].qps)
        result.extrapolated = False
        return result

    # 扫描交点：分段线性插值求 P95 = threshold
    for i in range(1, end):
        prev, cur = curve[i - 1], curve[i]
        if cur.p95_ms > threshold:
            ratio = (threshold - prev.p95_ms) / (cur.p95_ms - prev.p95_ms)
            concurrency = prev.concurrency + ratio * (cur.concurrency - prev.concurrency)
            qps = prev.qps + ratio * (cur.qps - prev.qps)
            result.max_concurrency = int(max(1, math.floor(concurrency)))
            result.max_qps = int(max(0, math.floor(qps)))
            result.extrapolated = False
            return result

    # 全程未达阈值：容量未触顶，结果为测试上限（有效档位内）
    last = curve[end - 1]
    result.max_concurrency = last.concurrency
    result.max_qps = int(last.qps)
    result.extrapolated = True
    return result


def _fill_science(result: CapacityResult) -> None:
    """科学化模型补充；采样档 <3 或拟合失败时静默跳过（不影响既有判定）。"""
    curve = result.curve
    if len(curve) < 3:
        return
    end = _effective_end(curve)
    if end < 3:
        return
    xs = [float(p.concurrency) for p in curve[:end]]
    qps_ys = [p.qps for p in curve[:end]]
    rt_ys = [p.p95_ms for p in curve[:end]]

    # 1) RT-并发二阶导拐点
    knee = knee_concurrency(xs, rt_ys)
    result.rt_knee_concurrency = 0 if math.isnan(knee) else knee

    # 2) 二项式拟合
    fit = fit_quadratic(xs, qps_ys)
    if fit is None:
        return
    result.model = FitModel(
        a=_round4(fit.a),
        b=_round4(fit.b),
        k=_round4(fit.k),
        r2=_round4(fit.r2),
        points=fit.points,
        vertex_concurrency=_round2(fit.vertex_concurrency) if fit.has_vertex else None,
        vertex_qps=_round2(fit.vertex_qps) if fit.has_vertex else None,
        formula=f"qps = {fit.a:.4f}·c² + {fit.b:.4f}·c + {fit.k:.4f}",
    )
    result.r2 = result.model.r2

    # 3) 拟合曲线：实测并发区间等距重采样
    mn, mx = xs[0], xs[end - 1]
    steps = max(2, FITTED_POINTS)
    result.fitted_curve = [
        FittedPoint(
            concurrency=_round2(mn + (mx - mn) * i / (steps - 1.0)),
            qps=_round2(fit.qps_at(mn + (mx - mn) * i / (steps - 1.0))),
        )
        for i in range(steps)
    ]

    # 4) 各档 95% 置信区间
    cis: list[ConfidenceInterval] = []
    for p in curve[:end]:
        seconds = p.total_requests / p.qps if p.qps > 0 else 0.0
        qps_lo, qps_hi = qps_interval(p.qps, seconds)
        rt_lo, rt_hi = p95_interval(p.p50_ms, p.p95_ms, p.p99_ms, p.total_requests)
        cis.append(
            ConfidenceInterval(
                concurrency=p.concurrency,
                qps=p.qps,
                qps_samples=_round2(max(0.0, seconds)),
                qps_lower=_round2(qps_lo),
                qps_upper=_round2(qps_hi),
                p95_ms=p.p95_ms,
                p95_lower=_round2(rt_lo),
                p95_upper=_round2(rt_hi),
            )
        )
    result.confidence_interval = cis


def analyze(points: list[StagePoint], target_p95_ms: float) -> CapacityResult:
    """分析容量：分段线性插值 + 科学化模型补充。"""
    result = _interpolate(points, target_p95_ms)
    _fill_science(result)
    return result
