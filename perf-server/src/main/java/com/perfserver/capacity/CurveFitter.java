package com.perfserver.capacity;

/**
 * 容量曲线拟合工具：二项式最小二乘拟合（{@code qps = a·c² + b·c + k}）、拟合优度 R²、
 * RT-并发曲线二阶导拐点检测，以及分档采样的 95% 置信区间。
 *
 * <p>纯静态数值工具：无外部依赖、无状态，单测可直接构造理论曲线验证（见
 * {@code CapacityAnalyzerScienceTest}）。拟合解 3×3 正规方程组（高斯消元 + 列主元），
 * 不引入任何数学库依赖。
 *
 * <p>统计口径说明：
 * <ul>
 *   <li><b>QPS 置信区间</b>：压测到达过程按泊松过程近似，每秒请求数方差 ≈ 均值，
 *       故 σ = √qps，n = 该档采样点数（采样秒数 = totalRequests / qps），
 *       95% CI = qps ± 1.96·σ/√n。</li>
 *   <li><b>RT 置信区间</b>：分档仅落库 P50/P95/P99，σ 由分位数反推
 *       （σ = (P99-P95)/0.6814，缺失 P99 时退化为 (P95-P50)/1.6449），
 *       n = 该档请求数，95% CI = P95 ± 1.96·σ/√n。</li>
 * </ul>
 */
public final class CurveFitter {

    /** 95% 置信区间 z 值 */
    public static final double Z_95 = 1.96;
    /** P99 与 P95 对应正态分位数之差（2.3263 - 1.6449），用于由分位数反推 σ */
    private static final double SIGMA_COEF_P99_P95 = 0.6814;
    /** P95 与 P50（≈均值）对应正态分位数之差，σ 反推的退化分支 */
    private static final double SIGMA_COEF_P95_P50 = 1.6449;
    /** 判定矩阵奇异的阈值 */
    private static final double EPS = 1e-9;

    private CurveFitter() {
    }

    /**
     * 二项式拟合结果：{@code qps = a·c² + b·c + k}。
     */
    public static class QuadraticFit {
        private final double a;
        private final double b;
        private final double k;
        private final double r2;
        /** 顶点并发（a&lt;0 时为理论最大并发），无顶点时为 NaN */
        private final double vertexConcurrency;
        /** 顶点 QPS（理论峰值吞吐） */
        private final double vertexQps;
        /** 参与拟合的采样点数 */
        private final int points;

        private QuadraticFit(double a, double b, double k, double r2,
                             double vertexConcurrency, double vertexQps, int points) {
            this.a = a;
            this.b = b;
            this.k = k;
            this.r2 = r2;
            this.vertexConcurrency = vertexConcurrency;
            this.vertexQps = vertexQps;
            this.points = points;
        }

        public double getA() {
            return a;
        }

        public double getB() {
            return b;
        }

        public double getK() {
            return k;
        }

        public double getR2() {
            return r2;
        }

        public double getVertexConcurrency() {
            return vertexConcurrency;
        }

        public double getVertexQps() {
            return vertexQps;
        }

        public int getPoints() {
            return points;
        }

        /** 是否存在拐点（a&lt;0 抛物线开口向下，顶点即理论最大并发） */
        public boolean hasVertex() {
            return !Double.isNaN(vertexConcurrency);
        }

        /** 拟合曲线在指定并发处的 QPS */
        public double qpsAt(double concurrency) {
            return a * concurrency * concurrency + b * concurrency + k;
        }
    }

    /**
     * 二项式最小二乘拟合（要求 ≥3 个不同并发采样点，否则返回 null）。
     *
     * @param xs 并发数
     * @param ys 对应 QPS
     * @return 拟合结果（系数 / R² / 顶点），数据不足或矩阵奇异时返回 null
     */
    public static QuadraticFit fitQuadratic(double[] xs, double[] ys) {
        if (xs == null || ys == null || xs.length < 3 || xs.length != ys.length) {
            return null;
        }
        int n = xs.length;
        // 正规方程组：[[Σx⁴,Σx³,Σx²],[Σx³,Σx²,Σx],[Σx²,Σx,n]]·[a,b,k]ᵀ = [Σx²y,Σxy,Σy]ᵀ
        double sx = 0, sx2 = 0, sx3 = 0, sx4 = 0, sy = 0, sxy = 0, sx2y = 0;
        for (int i = 0; i < n; i++) {
            double x = xs[i];
            double y = ys[i];
            double x2 = x * x;
            sx += x;
            sx2 += x2;
            sx3 += x2 * x;
            sx4 += x2 * x2;
            sy += y;
            sxy += x * y;
            sx2y += x2 * y;
        }
        double[][] m = {
                {sx4, sx3, sx2, sx2y},
                {sx3, sx2, sx, sxy},
                {sx2, sx, n, sy}
        };
        double[] coeff = solve3(m);
        if (coeff == null) {
            return null;
        }
        double a = coeff[0];
        double b = coeff[1];
        double k = coeff[2];

        double mean = sy / n;
        double sse = 0;
        double sst = 0;
        for (int i = 0; i < n; i++) {
            double fit = a * xs[i] * xs[i] + b * xs[i] + k;
            double d = ys[i] - fit;
            sse += d * d;
            double dm = ys[i] - mean;
            sst += dm * dm;
        }
        double r2 = sst <= EPS ? 1.0 : 1.0 - sse / sst;

        double vertex = Double.NaN;
        double vertexQps = Double.NaN;
        if (a < 0) {
            vertex = -b / (2 * a);
            vertexQps = a * vertex * vertex + b * vertex + k;
        }
        return new QuadraticFit(a, b, k, r2, vertex, vertexQps, n);
    }

    /**
     * RT-并发曲线二阶导拐点检测（最大曲率点）：非均匀网格二阶差分，
     * 取二阶导最大（RT 上翘最陡）处的并发，即容量拐点。
     *
     * @param xs 并发数（升序）
     * @param ys 对应 RT（P95 ms）
     * @return 拐点并发；无上翘（曲线线性/已饱和）或点数不足时返回 NaN
     */
    public static double kneeConcurrency(double[] xs, double[] ys) {
        if (xs == null || ys == null || xs.length < 3 || xs.length != ys.length) {
            return Double.NaN;
        }
        int n = xs.length;
        double best = 0;
        int bestIdx = -1;
        for (int i = 1; i < n - 1; i++) {
            double h1 = xs[i] - xs[i - 1];
            double h2 = xs[i + 1] - xs[i];
            if (h1 <= 0 || h2 <= 0) {
                continue;
            }
            double slope1 = (ys[i] - ys[i - 1]) / h1;
            double slope2 = (ys[i + 1] - ys[i]) / h2;
            // 非均匀网格二阶导：2·( slope2 - slope1 ) / (h1 + h2)
            double d2 = 2 * (slope2 - slope1) / (h1 + h2);
            if (d2 > best) {
                best = d2;
                bestIdx = i;
            }
        }
        return bestIdx < 0 ? Double.NaN : xs[bestIdx];
    }

    /**
     * 分档 QPS 的 95% 置信区间（泊松到达假设：σ = √qps，n = 该档采样点数）。
     *
     * @param qps     该档 QPS
     * @param samples 该档采样点数（采样秒数）
     * @return {lower, upper}；样本不足时为 {qps, qps}
     */
    public static double[] qpsInterval(double qps, double samples) {
        if (qps <= 0 || samples <= 1) {
            return new double[]{qps, qps};
        }
        double half = Z_95 * Math.sqrt(qps) / Math.sqrt(samples);
        return new double[]{Math.max(0, qps - half), qps + half};
    }

    /**
     * 分档 P95 RT 的 95% 置信区间（σ 由分位数反推，n = 该档请求数）。
     *
     * @param p50Ms   该档 P50（缺失传 0）
     * @param p95Ms   该档 P95
     * @param p99Ms   该档 P99（缺失传 0）
     * @param samples 该档请求数
     * @return {lower, upper}；样本不足时为 {p95Ms, p95Ms}
     */
    public static double[] p95Interval(double p50Ms, double p95Ms, double p99Ms, long samples) {
        if (p95Ms <= 0 || samples <= 1) {
            return new double[]{p95Ms, p95Ms};
        }
        double sigma;
        if (p99Ms > p95Ms) {
            sigma = (p99Ms - p95Ms) / SIGMA_COEF_P99_P95;
        } else if (p95Ms > p50Ms && p50Ms > 0) {
            sigma = (p95Ms - p50Ms) / SIGMA_COEF_P95_P50;
        } else {
            return new double[]{p95Ms, p95Ms};
        }
        double half = Z_95 * sigma / Math.sqrt(samples);
        return new double[]{Math.max(0, p95Ms - half), p95Ms + half};
    }

    /** 解 3×3 线性方程组（增广矩阵 3 行 4 列，高斯消元 + 列主元）；奇异返回 null */
    private static double[] solve3(double[][] m) {
        for (int col = 0; col < 3; col++) {
            int pivot = col;
            for (int r = col + 1; r < 3; r++) {
                if (Math.abs(m[r][col]) > Math.abs(m[pivot][col])) {
                    pivot = r;
                }
            }
            if (Math.abs(m[pivot][col]) < EPS) {
                return null;
            }
            if (pivot != col) {
                double[] tmp = m[pivot];
                m[pivot] = m[col];
                m[col] = tmp;
            }
            for (int r = col + 1; r < 3; r++) {
                double factor = m[r][col] / m[col][col];
                for (int c = col; c < 4; c++) {
                    m[r][c] -= factor * m[col][c];
                }
            }
        }
        double[] x = new double[3];
        for (int r = 2; r >= 0; r--) {
            double sum = m[r][3];
            for (int c = r + 1; c < 3; c++) {
                sum -= m[r][c] * x[c];
            }
            x[r] = sum / m[r][r];
            if (Double.isNaN(x[r]) || Double.isInfinite(x[r])) {
                return null;
            }
        }
        return x;
    }
}
