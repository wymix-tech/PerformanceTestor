package com.perfserver.stress;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
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

    /**
     * 分步骤累计统计（多接口场景编排用）：key = 步骤名。
     *
     * <p>用独立的轻量计数器而非完整直方图：分步骤只需请求数/错误数/耗时累计
     * （分位数由引擎侧在场景模式下按需单独采集），避免 30000 桶 × 步数的内存放大。
     */
    private final Map<String, StepCounter> stepCounters = new ConcurrentHashMap<>();

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
     * 记录一次请求耗时并归入指定步骤（多接口场景编排用）。
     *
     * <p>汇总侧与 {@link #record} 完全一致（同一份直方图），
     * 额外按步骤名累加一份轻量计数，读取时合成分步骤明细。
     *
     * @param stepName 步骤名（null/空则只计汇总）
     */
    public void record(long costMs, boolean error, String stepName) {
        record(costMs, error, stepName, 0);
    }

    /**
     * 记录一次请求耗时并归入指定步骤，同时累计该次迭代的上下文引用未命中数。
     *
     * @param stepName   步骤名（null/空则只计汇总）
     * @param renderMiss 本次迭代渲染模板时「确定的上下文引用但取不到值」的次数；
     *                   由调用方按迭代取零后传入（warmup/stop 期的未命中不应计入，
     *                   故计数在渲染侧清零、随本次 record 一次性提交）
     */
    public void record(long costMs, boolean error, String stepName, long renderMiss) {
        record(costMs, error);
        if (stepName != null && !stepName.isEmpty()) {
            stepCounters.computeIfAbsent(stepName, k -> new StepCounter())
                    .record(costMs, error, renderMiss);
        }
    }

    /**
     * 分步骤统计快照（按进入顺序稳定输出；未记录任何步骤时返回空列表）。
     *
     * @param formalElapsedSec 正式期时长（秒），用于计算各步骤 QPS
     */
    public List<StepStats> stepStats(double formalElapsedSec) {
        if (stepCounters.isEmpty()) {
            return List.of();
        }
        double elapsed = Math.max(1.0, formalElapsedSec);
        List<StepStats> list = new ArrayList<>(stepCounters.size());
        stepCounters.forEach((name, c) -> {
            long total = c.total.sum();
            long errors = c.errors.sum();
            StepStats s = new StepStats();
            s.stepName = name;
            s.totalRequests = total;
            s.errorCount = errors;
            s.errorRate = total == 0 ? 0 : round2(errors * 100.0 / total);
            s.qps = round2(total / elapsed);
            s.avgMs = total == 0 ? 0 : round2((double) c.costMs.sum() / total);
            s.maxMs = c.max.get();
            s.renderMiss = c.renderMiss.sum();
            list.add(s);
        });
        list.sort((a, b) -> a.stepName.compareTo(b.stepName));
        return list;
    }

    /** 是否记录过任何分步骤数据 */
    public boolean hasStepStats() {
        return !stepCounters.isEmpty();
    }

    private static double round2(double v) {
        return Math.round(v * 100.0) / 100.0;
    }

    /** 单步骤轻量计数器（LongAdder 无锁累加 + max 用 CAS 更新） */
    private static final class StepCounter {
        private final LongAdder total = new LongAdder();
        private final LongAdder errors = new LongAdder();
        private final LongAdder costMs = new LongAdder();
        /** 上下文引用未命中累计（{{pN.result.x}} 取不到值的次数） */
        private final LongAdder renderMiss = new LongAdder();
        private final java.util.concurrent.atomic.AtomicLong max = new java.util.concurrent.atomic.AtomicLong();

        void record(long cost, boolean error, long miss) {
            total.increment();
            costMs.add(cost);
            if (error) {
                errors.increment();
            }
            if (miss > 0) {
                renderMiss.add(miss);
            }
            max.accumulateAndGet(cost, Math::max);
        }
    }

    /** 分步骤统计结果 */
    public static class StepStats {
        private String stepName;
        private long totalRequests;
        private long errorCount;
        private double errorRate;
        private double qps;
        private double avgMs;
        private double maxMs;
        /** 上下文引用未命中次数（{@code {{pN.result.x}}} 等取不到值时累加） */
        private long renderMiss;

        public String getStepName() {
            return stepName;
        }

        public long getTotalRequests() {
            return totalRequests;
        }

        public long getErrorCount() {
            return errorCount;
        }

        public double getErrorRate() {
            return errorRate;
        }

        public double getQps() {
            return qps;
        }

        public double getAvgMs() {
            return avgMs;
        }

        public double getMaxMs() {
            return maxMs;
        }

        public long getRenderMiss() {
            return renderMiss;
        }

        /** 由聚合结果构造（分档合并场景） */
        static StepStats of(String stepName, long total, long errors,
                            long totalCostMs, long maxMs, long renderMiss) {
            StepStats s = new StepStats();
            s.stepName = stepName;
            s.totalRequests = total;
            s.errorCount = errors;
            s.errorRate = total == 0 ? 0 : round2(errors * 100.0 / total);
            s.qps = 0; // 分档合并场景由调用方按总时长换算
            s.avgMs = total == 0 ? 0 : round2((double) totalCostMs / total);
            s.maxMs = maxMs;
            s.renderMiss = renderMiss;
            return s;
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
