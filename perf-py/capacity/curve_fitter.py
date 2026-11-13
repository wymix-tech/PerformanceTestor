"""容量曲线拟合工具：移植自 CurveFitter.java，算法逐行对齐。

- 二项式最小二乘拟合 qps = a·c² + b·c + k（3×3 正规方程组，高斯消元 + 列主元，无数学库依赖）
- 拟合优度 R²、顶点（理论最大并发）
- RT-并发曲线二阶导拐点检测（非均匀网格二阶差分）
- 分档 95% 置信区间（泊松到达假设；RT 由分位数反推 σ）

统计口径（与 Java 侧注释一致）：
- QPS CI：σ = √qps，n = 该档采样点数（采样秒数 = totalRequests / qps）
- RT CI：σ = (P99-P95)/0.6814，缺失 P99 时退化为 (P95-P50)/1.6449
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Z_95 = 1.96
SIGMA_COEF_P99_P95 = 0.6814  # 2.3263 - 1.6449
SIGMA_COEF_P95_P50 = 1.6449
EPS = 1e-9

NAN = float("nan")


@dataclass(frozen=True)
class QuadraticFit:
    """二项式拟合结果：qps = a·c² + b·c + k。"""

    a: float
    b: float
    k: float
    r2: float
    vertex_concurrency: float  # a<0 时为理论最大并发，否则 NaN
    vertex_qps: float
    points: int

    @property
    def has_vertex(self) -> bool:
        return not math.isnan(self.vertex_concurrency)

    def qps_at(self, concurrency: float) -> float:
        return self.a * concurrency * concurrency + self.b * concurrency + self.k


def _solve3(m: list[list[float]]) -> list[float] | None:
    """解 3×3 线性方程组（增广矩阵，高斯消元 + 列主元）；奇异返回 None。"""
    for col in range(3):
        pivot = col
        for r in range(col + 1, 3):
            if abs(m[r][col]) > abs(m[pivot][col]):
                pivot = r
        if abs(m[pivot][col]) < EPS:
            return None
        if pivot != col:
            m[pivot], m[col] = m[col], m[pivot]
        for r in range(col + 1, 3):
            factor = m[r][col] / m[col][col]
            for c in range(col, 4):
                m[r][c] -= factor * m[col][c]
    x = [0.0, 0.0, 0.0]
    for r in range(2, -1, -1):
        s = m[r][3]
        for c in range(r + 1, 3):
            s -= m[r][c] * x[c]
        x[r] = s / m[r][r]
        if math.isnan(x[r]) or math.isinf(x[r]):
            return None
    return x


def fit_quadratic(xs: list[float], ys: list[float]) -> QuadraticFit | None:
    """二项式最小二乘拟合（要求 ≥3 个不同并发采样点，否则返回 None）。"""
    if xs is None or ys is None or len(xs) < 3 or len(xs) != len(ys):
        return None
    n = len(xs)
    sx = sx2 = sx3 = sx4 = sy = sxy = sx2y = 0.0
    for x, y in zip(xs, ys, strict=False):
        x2 = x * x
        sx += x
        sx2 += x2
        sx3 += x2 * x
        sx4 += x2 * x2
        sy += y
        sxy += x * y
        sx2y += x2 * y
    # 正规方程组：[[Σx⁴,Σx³,Σx²],[Σx³,Σx²,Σx],[Σx²,Σx,n]]·[a,b,k]ᵀ = [Σx²y,Σxy,Σy]ᵀ
    m = [
        [sx4, sx3, sx2, sx2y],
        [sx3, sx2, sx, sxy],
        [sx2, sx, float(n), sy],
    ]
    coeff = _solve3(m)
    if coeff is None:
        return None
    a, b, k = coeff

    mean = sy / n
    sse = sst = 0.0
    for x, y in zip(xs, ys, strict=False):
        fit = a * x * x + b * x + k
        sse += (y - fit) ** 2
        sst += (y - mean) ** 2
    r2 = 1.0 if sst <= EPS else 1.0 - sse / sst

    vertex = vertex_qps = NAN
    if a < 0:
        vertex = -b / (2 * a)
        vertex_qps = a * vertex * vertex + b * vertex + k
    return QuadraticFit(a, b, k, r2, vertex, vertex_qps, n)


def knee_concurrency(xs: list[float], ys: list[float]) -> float:
    """RT-并发曲线二阶导拐点（最大曲率点）；无上翘或点数不足返回 NaN。"""
    if xs is None or ys is None or len(xs) < 3 or len(xs) != len(ys):
        return NAN
    n = len(xs)
    best = 0.0
    best_idx = -1
    for i in range(1, n - 1):
        h1 = xs[i] - xs[i - 1]
        h2 = xs[i + 1] - xs[i]
        if h1 <= 0 or h2 <= 0:
            continue
        slope1 = (ys[i] - ys[i - 1]) / h1
        slope2 = (ys[i + 1] - ys[i]) / h2
        # 非均匀网格二阶导：2·(slope2 - slope1) / (h1 + h2)
        d2 = 2 * (slope2 - slope1) / (h1 + h2)
        if d2 > best:
            best = d2
            best_idx = i
    return NAN if best_idx < 0 else xs[best_idx]


def qps_interval(qps: float, samples: float) -> tuple[float, float]:
    """分档 QPS 95% 置信区间（泊松到达假设）；样本不足时为 (qps, qps)。"""
    if qps <= 0 or samples <= 1:
        return qps, qps
    half = Z_95 * math.sqrt(qps) / math.sqrt(samples)
    return max(0.0, qps - half), qps + half


def p95_interval(p50_ms: float, p95_ms: float, p99_ms: float, samples: int) -> tuple[float, float]:
    """分档 P95 RT 95% 置信区间（σ 由分位数反推）；样本不足时为 (p95, p95)。"""
    if p95_ms <= 0 or samples <= 1:
        return p95_ms, p95_ms
    if p99_ms > p95_ms:
        sigma = (p99_ms - p95_ms) / SIGMA_COEF_P99_P95
    elif p95_ms > p50_ms > 0:
        sigma = (p95_ms - p50_ms) / SIGMA_COEF_P95_P50
    else:
        return p95_ms, p95_ms
    half = Z_95 * sigma / math.sqrt(samples)
    return max(0.0, p95_ms - half), p95_ms + half
