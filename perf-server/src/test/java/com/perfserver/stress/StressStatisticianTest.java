package com.perfserver.stress;

import org.junit.jupiter.api.Test;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * StressStatistician 无锁化重构正确性测试：
 * 多线程 record 后累计/窗口/分位数与单线程参照一致。
 */
class StressStatisticianTest {

    @Test
    void multiThreadRecordMatchesSingleThreadReference() throws Exception {
        StressStatistician stats = new StressStatistician();
        int threads = 8;
        int perThread = 2000;
        CountDownLatch latch = new CountDownLatch(threads);
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        for (int t = 0; t < threads; t++) {
            final int tid = t;
            pool.submit(() -> {
                try {
                    for (int i = 0; i < perThread; i++) {
                        // 每线程产生确定性耗时：0-499ms 循环
                        long cost = (tid * perThread + i) % 500;
                        stats.record(cost, (tid + i) % 7 == 0);
                    }
                } finally {
                    latch.countDown();
                }
            });
        }
        assertTrue(latch.await(10, TimeUnit.SECONDS));
        pool.shutdown();

        StressStatistician.StageSnapshot snap = stats.stageSnapshot();
        assertEquals(threads * perThread, snap.getTotal());
        // 错误数：每线程内 i % 7 == (7 - tid%7) % 7 的命中次数 —— 直接用总数校验过于繁琐，
        // 用确定性参照重建：单线程顺序回放同一序列
        StressStatistician ref = new StressStatistician();
        for (int t = 0; t < threads; t++) {
            for (int i = 0; i < perThread; i++) {
                ref.record((t * perThread + i) % 500, (t + i) % 7 == 0);
            }
        }
        StressStatistician.StageSnapshot refSnap = ref.stageSnapshot();
        assertEquals(refSnap.getTotal(), snap.getTotal());
        assertEquals(refSnap.getErrorCount(), snap.getErrorCount());
        assertEquals(refSnap.getTotalCostMs(), snap.getTotalCostMs());
        // 分位数一致（同一直方图）
        assertEquals(StressStatistician.percentile(refSnap.getHistogram(), 50),
                StressStatistician.percentile(snap.getHistogram(), 50), 0.0);
        assertEquals(StressStatistician.percentile(refSnap.getHistogram(), 95),
                StressStatistician.percentile(snap.getHistogram(), 95), 0.0);
        assertEquals(StressStatistician.percentile(refSnap.getHistogram(), 99),
                StressStatistician.percentile(snap.getHistogram(), 99), 0.0);
    }

    @Test
    void windowDiffIsIndependentPerPeriod() {
        StressStatistician stats = new StressStatistician();
        for (int i = 0; i < 10; i++) {
            stats.record(5, false);
        }
        StressStatistician.WindowStats w1 = stats.collectWindow();
        assertEquals(10, w1.getCount());

        for (int i = 0; i < 5; i++) {
            stats.record(7, true);
        }
        StressStatistician.WindowStats w2 = stats.collectWindow();
        assertEquals(5, w2.getCount());
        assertEquals(5, w2.getErrorCount());
        assertEquals(7, StressStatistician.percentile(w2.getHistogram(), 50), 0.0);

        // 空窗口
        StressStatistician.WindowStats w3 = stats.collectWindow();
        assertEquals(0, w3.getCount());
    }

    @Test
    void percentileAndMaxOnKnownHistogram() {
        // 98 个 10ms + 2 个 3000ms：nearest-rank 语义下 P50/P95=10、P99=3000（第 99 个有序值）
        StressStatistician stats = new StressStatistician();
        for (int i = 0; i < 98; i++) {
            stats.record(10, false);
        }
        stats.record(3000, false);
        stats.record(3000, false);
        StressStatistician.StageSnapshot snap = stats.stageSnapshot();
        assertEquals(10, StressStatistician.percentile(snap.getHistogram(), 50), 0.0);
        assertEquals(10, StressStatistician.percentile(snap.getHistogram(), 95), 0.0);
        assertEquals(3000, StressStatistician.percentile(snap.getHistogram(), 99), 0.0);
        assertEquals(3000, StressStatistician.max(snap.getHistogram()), 0.0);
    }

    @Test
    void overflowCostClampsToLastBucket() {
        StressStatistician stats = new StressStatistician();
        stats.record(60_000, false); // 超出 30s 覆盖范围 → 末桶
        StressStatistician.StageSnapshot snap = stats.stageSnapshot();
        assertEquals(29999, StressStatistician.max(snap.getHistogram()), 0.0);
    }

    /** renderMiss 按步骤累加；未传该参数的三参重载保持 0（存量行为不变） */
    @Test
    void renderMissAccumulatesPerStep() {
        StressStatistician stats = new StressStatistician();
        stats.record(10, false, "order", 2);
        stats.record(10, false, "order", 0);
        stats.record(10, true, "order", 3);
        stats.record(10, false, "login", 0);
        stats.record(10, false, "legacy"); // 三参重载：不传 miss，保持存量行为

        StressStatistician.StepStats order = byName(stats.stepStats(1.0), "order");
        assertEquals(5, order.getRenderMiss());
        assertEquals(3, order.getTotalRequests());
        assertEquals(0, byName(stats.stepStats(1.0), "login").getRenderMiss());
        assertEquals(0, byName(stats.stepStats(1.0), "legacy").getRenderMiss());
    }

    /** 分档合并口径：StepStats.of 透传 renderMiss 后可跨档直接相加 */
    @Test
    void renderMissSurvivesMergeFactory() {
        StressStatistician.StepStats a =
                StressStatistician.StepStats.of("order", 10, 1, 100, 20, 4);
        StressStatistician.StepStats b =
                StressStatistician.StepStats.of("order", 20, 2, 400, 60, 6);
        assertEquals(4, a.getRenderMiss());
        assertEquals(6, b.getRenderMiss());
        // 引擎侧 mergeStepStats 用 StepAccumulator 累加，此处只校验工厂透传
        assertEquals(a.getRenderMiss() + b.getRenderMiss(), 10);
    }

    private static StressStatistician.StepStats byName(java.util.List<StressStatistician.StepStats> stats,
                                                       String name) {
        return stats.stream().filter(s -> s.getStepName().equals(name)).findFirst().orElseThrow();
    }
}
