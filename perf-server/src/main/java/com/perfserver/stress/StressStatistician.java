package com.perfserver.stress;

import java.util.concurrent.atomic.AtomicLongArray;
import java.util.concurrent.atomic.LongAdder;

/**
 * 压测统计器：LongAdder 计数 + 分片直方图，写入无锁，内存 O(shards)。
 *
 * <p>直方图桶宽 1ms，覆盖 [0, 30000ms)，超出部分落入最后一个桶，用于
 * P50/P95/P99 分位数计算。窗口采样基于累计直方图差值，保证各采样周期独立。
 *
 * <p><b>无锁化设计</b>（替代旧的全局 synchronized——5000 worker 争抢一把锁，
 * 高 QPS 下统计自身成为瓶颈）：
 * <ul>
 *   <li>计数器用 {@link LongAdder}（分段累加，读取时求和）；</li>
 *   <li>直方图按线程哈希分 {@value #SHARD_COUNT} 片，每片 {@link AtomicLongArray}
 *       独立 CAS 自增——争用面缩小为同片同桶，跨片零竞争；</li>
 *   <li>collectWindow/stageSnapshot 无锁合并读（读取瞬间可能有微小不一致，
 *       对监控统计可接受）；对外 API 签名与语义（30000 桶 × 1ms）保持不变。</li>
 * </ul>
 */
public class StressStatistician {

    /** 桶数量：覆盖 0-29999ms，最后一个桶收纳溢出值 */
    private static final int BUCKET_COUNT = 30000;
    /** 直方图分片数（2 的幂，线程哈希取模） */
    private static final int SHARD_COUNT = 16;
    private static final int SHARD_MASK = SHARD_COUNT - 1;

    /** 分片累计直方图：histograms[shard][bucket] */
    private final AtomicLongArray[] histograms = new AtomicLongArray[SHARD_COUNT];
    /** 分片选择缓存（每线程一个下标，避免每次取 Thread.getId） */
    private final ThreadLocal<Integer> shardHolder = ThreadLocal.withInitial(
            () -> (int) (Thread.currentThread().getId() & SHARD_MASK));

    private final LongAdder totalCount = new LongAdder();
    private final LongAdder errorCount = new LongAdder();
    private final LongAdder totalCostMs = new LongAdder();

    /** 窗口基线（上次 collectWindow 时的合并累计快照） */
    private final long[] lastHistogram = new long[BUCKET_COUNT];
    private long lastTotal;
    private long lastError;
    private long lastCostMs;

    public StressStatistician() {
        for (int i = 0; i < SHARD_COUNT; i++) {
            histograms[i] = new AtomicLongArray(BUCKET_COUNT);
        }
    }

    /** 记录一次请求耗时（毫秒）与是否错误（无锁：分片 CAS + LongAdder） */
    public void record(long costMs, boolean error) {
        int idx = (int) Math.min(Math.max(costMs, 0), BUCKET_COUNT - 1);
        histograms[shardHolder.get()].incrementAndGet(idx);
        totalCount.increment();
        totalCostMs.add(costMs);
        if (error) {
            errorCount.increment();
        }
    }

    /**
     * 收集自上次调用以来的窗口统计（独立周期），并更新基线（无锁合并读）。
     */
    public WindowStats collectWindow() {
        long[] merged = mergedHistogram();
        long[] window = new long[BUCKET_COUNT];
        for (int i = 0; i < BUCKET_COUNT; i++) {
            window[i] = merged[i] - lastHistogram[i];
        }
        long count = totalCount.sum() - lastTotal;
        long errors = errorCount.sum() - lastError;
        long costMs = totalCostMs.sum() - lastCostMs;

        System.arraycopy(merged, 0, lastHistogram, 0, BUCKET_COUNT);
        lastTotal = totalCount.sum();
        lastError = errorCount.sum();
        lastCostMs = totalCostMs.sum();

        WindowStats stats = new WindowStats();
        stats.count = count;
        stats.errorCount = errors;
        stats.totalCostMs = costMs;
        stats.histogram = window;
        return stats;
    }

    /** 当前累计快照（用于分档统计与最终结果；无锁合并读） */
    public StageSnapshot stageSnapshot() {
        StageSnapshot snap = new StageSnapshot();
        snap.total = totalCount.sum();
        snap.errorCount = errorCount.sum();
        snap.totalCostMs = totalCostMs.sum();
        snap.histogram = mergedHistogram();
        return snap;
    }

    /** 合并全部分片直方图为单份数组（读取路径，调用频率低） */
    private long[] mergedHistogram() {
        long[] merged = new long[BUCKET_COUNT];
        for (int s = 0; s < SHARD_COUNT; s++) {
            AtomicLongArray shard = histograms[s];
            for (int i = 0; i < BUCKET_COUNT; i++) {
                long v = shard.get(i);
                if (v != 0) {
                    merged[i] += v;
                }
            }
        }
        return merged;
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
