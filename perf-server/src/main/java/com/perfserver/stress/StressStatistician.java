package com.perfserver.stress;

import java.util.Arrays;

/**
 * 压测统计器：累加器 + 固定桶直方图，内存 O(1)。
 *
 * <p>直方图桶宽 1ms，覆盖 [0, 30000ms)，超出部分落入最后一个桶，用于
 * P50/P95/P99 分位数计算。窗口采样基于累计直方图差值，保证各采样周期独立。
 */
public class StressStatistician {

    /** 桶数量：覆盖 0-29999ms，最后一个桶收纳溢出值 */
    private static final int BUCKET_COUNT = 30000;

    /** 累计直方图 */
    private final long[] histogram = new long[BUCKET_COUNT];
    private long totalCount;
    private long errorCount;
    private long totalCostMs;

    /** 窗口基线（上次 collectWindow 时的累计快照） */
    private final long[] lastHistogram = new long[BUCKET_COUNT];
    private long lastTotal;
    private long lastError;
    private long lastCostMs;

    /** 记录一次请求耗时（毫秒）与是否错误 */
    public synchronized void record(long costMs, boolean error) {
        int idx = (int) Math.min(Math.max(costMs, 0), BUCKET_COUNT - 1);
        histogram[idx]++;
        totalCount++;
        totalCostMs += costMs;
        if (error) {
            errorCount++;
        }
    }

    /**
     * 收集自上次调用以来的窗口统计（独立周期），并更新基线。
     */
    public synchronized WindowStats collectWindow() {
        long[] window = new long[BUCKET_COUNT];
        for (int i = 0; i < BUCKET_COUNT; i++) {
            window[i] = histogram[i] - lastHistogram[i];
        }
        long count = totalCount - lastTotal;
        long errors = errorCount - lastError;
        long costMs = totalCostMs - lastCostMs;

        System.arraycopy(histogram, 0, lastHistogram, 0, BUCKET_COUNT);
        lastTotal = totalCount;
        lastError = errorCount;
        lastCostMs = totalCostMs;

        WindowStats stats = new WindowStats();
        stats.count = count;
        stats.errorCount = errors;
        stats.totalCostMs = costMs;
        stats.histogram = window;
        return stats;
    }

    /** 当前累计快照（用于分档统计与最终结果） */
    public synchronized StageSnapshot stageSnapshot() {
        StageSnapshot snap = new StageSnapshot();
        snap.total = totalCount;
        snap.errorCount = errorCount;
        snap.totalCostMs = totalCostMs;
        snap.histogram = Arrays.copyOf(histogram, BUCKET_COUNT);
        return snap;
    }

    /** 从直方图计算分位数（毫秒）：p 取值 50/95/99 等 */
    public static double percentile(long[] hist, double p) {
        long total = 0;
        for (long v : hist) {
            total += v;
        }
        if (total <= 0) {
            return 0;
        }
        double target = total * p / 100.0;
        long cum = 0;
        for (int i = 0; i < hist.length; i++) {
            cum += hist[i];
            if (cum >= target) {
                return i;
            }
        }
        return hist.length - 1;
    }

    /** 从直方图求最大值（毫秒）：最后一个非零桶 */
    public static double max(long[] hist) {
        for (int i = hist.length - 1; i >= 0; i--) {
            if (hist[i] > 0) {
                return i;
            }
        }
        return 0;
    }

    /** 周期窗口统计 */
    public static class WindowStats {
        private long count;
        private long errorCount;
        private long totalCostMs;
        private long[] histogram;

        public long getCount() {
            return count;
        }

        public long getErrorCount() {
            return errorCount;
        }

        public long getTotalCostMs() {
            return totalCostMs;
        }

        public long[] getHistogram() {
            return histogram;
        }
    }

    /** 阶段累计快照 */
    public static class StageSnapshot {
        private long total;
        private long errorCount;
        private long totalCostMs;
        private long[] histogram;

        public long getTotal() {
            return total;
        }

        public long getErrorCount() {
            return errorCount;
        }

        public long getTotalCostMs() {
            return totalCostMs;
        }

        public long[] getHistogram() {
            return histogram;
        }

        public double getAvgMs() {
            return total <= 0 ? 0 : (double) totalCostMs / total;
        }
    }
}
