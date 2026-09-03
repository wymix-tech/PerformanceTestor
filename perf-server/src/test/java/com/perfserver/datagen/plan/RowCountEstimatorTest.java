package com.perfserver.datagen.plan;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 行数推导单测：读/写/父表/权重分摊/分页下限/上限缩减。 */
class RowCountEstimatorTest {

    private static RowCountEstimator.StressParams params(int concurrency, int durationSec, double qpsLimit) {
        RowCountEstimator.StressParams p = new RowCountEstimator.StressParams();
        p.setConcurrency(concurrency);
        p.setDurationSec(durationSec);
        p.setQpsLimit(qpsLimit);
        return p;
    }

    private static RowCountEstimator.TableDemand read(String table) {
        return new RowCountEstimator.TableDemand(table, false, 20);
    }

    private static RowCountEstimator.TableDemand write(String table) {
        return new RowCountEstimator.TableDemand(table, true, 20);
    }

    @Test
    void peakQpsPrefersExplicitQpsLimit() {
        RowCountEstimator.StressParams p = params(100, 60, 500);
        assertEquals(500.0, RowCountEstimator.peakQps(p), 0.001);
    }

    @Test
    void peakQpsFallsBackToConcurrencyOverRt() {
        RowCountEstimator.StressParams p = params(100, 60, 0);
        p.setAssumedRtMs(50);
        // 100 * (1000 / 50) = 2000
        assertEquals(2000.0, RowCountEstimator.peakQps(p), 0.001);
    }

    @Test
    void slowerResponseTimeYieldsFewerRequests() {
        RowCountEstimator.StressParams fast = params(100, 60, 0);
        fast.setAssumedRtMs(20);
        RowCountEstimator.StressParams slow = params(100, 60, 0);
        slow.setAssumedRtMs(500);
        assertTrue(RowCountEstimator.totalRequests(slow, RowCountEstimator.peakQps(slow))
                < RowCountEstimator.totalRequests(fast, RowCountEstimator.peakQps(fast)),
                "慢接口的请求量必须更少，否则行数会被高估几十倍");
    }

    @Test
    void totalRequestsIsQpsTimesDuration() {
        RowCountEstimator.StressParams p = params(1, 60, 100);
        assertEquals(6000, RowCountEstimator.totalRequests(p, 100));
    }

    @Test
    void stagesAreSummed() {
        RowCountEstimator.StressParams p = params(1, 60, 0);
        p.setAssumedRtMs(100);
        RowCountEstimator.Stage s1 = new RowCountEstimator.Stage();
        s1.setConcurrency(10);
        s1.setDurationSec(60);
        RowCountEstimator.Stage s2 = new RowCountEstimator.Stage();
        s2.setConcurrency(20);
        s2.setDurationSec(60);
        p.setStages(List.of(s1, s2));
        // 10*10*60 + 20*10*60 = 6000 + 12000 = 18000
        double qps = RowCountEstimator.peakQps(p);
        assertEquals(18000, RowCountEstimator.totalRequests(p, qps));
    }

    @Test
    void weightsSplitRequestsProportionally() {
        RowCountEstimator.StressParams p = params(1, 60, 1000);
        // a 属于权重 3 的步骤，b 属于权重 1 的步骤（必须显式声明所属接口）
        var out = RowCountEstimator.estimate(p, List.of(
                new RowCountEstimator.TableDemand("a", false, 20, 0),
                new RowCountEstimator.TableDemand("b", false, 20, 1)), List.of(3, 1));
        assertEquals(60000, out.getTotalRequests());
        int a = out.getRowsByTable().get("a");
        int b = out.getRowsByTable().get("b");
        assertTrue(a > b, "权重高的步骤应得到更多行数: a=" + a + " b=" + b);
    }

    @Test
    void zeroWeightsFallBackToEvenSplit() {
        RowCountEstimator.StressParams p = params(1, 60, 1000);
        var out = RowCountEstimator.estimate(p, List.of(read("a"), read("b")), List.of(0, 0));
        assertEquals(out.getRowsByTable().get("a"), out.getRowsByTable().get("b"));
    }

    @Test
    void sameTableTakesMaxNotSum() {
        RowCountEstimator.StressParams p = params(1, 60, 100000);
        var out = RowCountEstimator.estimate(p, List.of(read("order"), read("order")), List.of(1, 1));
        int rows = out.getRowsByTable().get("order");
        int single = RowCountEstimator.estimate(p, List.of(read("order")), List.of(1))
                .getRowsByTable().get("order");
        assertEquals(single, rows, "同表多步骤命中应取 max，避免行数被放大");
    }

    @Test
    void writeTableGetsMoreRowsThanReadTable() {
        RowCountEstimator.StressParams p = params(1, 60, 10000);
        var out = RowCountEstimator.estimate(p, List.of(read("a"), write("b")), List.of(1, 1));
        assertTrue(out.getRowsByTable().get("b") > out.getRowsByTable().get("a"),
                "写表行数应大于读表（READ 只需 HIT_RATIO 命中数据量）");
    }

    @Test
    void pageSizeRaisesFloorForReadTable() {
        RowCountEstimator.StressParams p = params(1, 1, 1);
        int rows = RowCountEstimator.estimate(p, List.of(
                new RowCountEstimator.TableDemand("a", false, 100))).getRowsByTable().get("a");
        assertTrue(rows >= 100 * RowCountEstimator.PAGE_MULTIPLIER,
                "分页大时必须保证能翻到足够页数，实际: " + rows);
    }

    @Test
    void parentRowsIsChildDividedByAvgChildren() {
        assertEquals(1000, RowCountEstimator.parentRows(10000));
        assertEquals(RowCountEstimator.MIN_PARENT_ROWS, RowCountEstimator.parentRows(5),
                "父表行数有下限，避免父表过小导致 ref 池不够");
        assertEquals(RowCountEstimator.MAX_TABLE_ROWS, RowCountEstimator.parentRows(Integer.MAX_VALUE));
    }

    @Test
    void tinyWorkloadStillRespectsMinimum() {
        RowCountEstimator.StressParams p = params(1, 1, 1);
        int rows = RowCountEstimator.estimate(p, List.of(read("a")), List.of(1))
                .getRowsByTable().get("a");
        assertTrue(rows >= RowCountEstimator.MIN_MAIN_ROWS, "极小压力也有下限，实际: " + rows);
    }

    @Test
    void totalOverLimitIsShrunkWithWarning() {
        RowCountEstimator.StressParams p = params(1, 600, 1_000_000);
        // 6 张写表 × 单表上限 20 万 = 120 万 > 全局上限 100 万，触发缩减
        var out = RowCountEstimator.estimate(p,
                List.of(write("a"), write("b"), write("c"), write("d"), write("e"), write("f")),
                List.of(1, 1, 1, 1, 1, 1));
        long total = out.getRowsByTable().values().stream().mapToLong(Integer::longValue).sum();
        assertTrue(total <= RowCountEstimator.MAX_TOTAL_ROWS,
                "超过上限必须按比例缩减，实际: " + total);
        assertTrue(out.getWarnings().stream().anyMatch(w -> w.contains("超过上限")),
                "缩减必须给出告警，不能静默截断: " + out.getWarnings());
    }

    @Test
    void singleTableIsCappedAtMax() {
        RowCountEstimator.StressParams p = params(1, 60000, 1_000_000);
        int rows = RowCountEstimator.estimate(p, List.of(write("a")), List.of(1))
                .getRowsByTable().get("a");
        assertEquals(RowCountEstimator.MAX_TABLE_ROWS, rows);
    }

    @Test
    void emptyDemandsYieldsEmptyResult() {
        assertTrue(RowCountEstimator.estimate(params(1, 60, 100), List.of()).getRowsByTable().isEmpty());
    }

    // ===== 权重对齐（PR 评审修复：demands 数 ≠ 接口数，权重曾恒退化为均分） =====

    @Test
    void endpointWeightsApplyWhenDemandsOutnumberEndpoints() {
        // 2 个接口（权重 9:1），第一个接口访问了 3 张表、第二个访问 1 张表
        // 若按 demands.size() 对齐，权重数(2) ≠ 分摊对象数(4)，会恒退化为均分——必须不退化
        RowCountEstimator.StressParams p = params(1, 60, 100000);
        List<RowCountEstimator.TableDemand> demands = List.of(
                new RowCountEstimator.TableDemand("a", false, 20, 0),
                new RowCountEstimator.TableDemand("b", false, 20, 0),
                new RowCountEstimator.TableDemand("c", false, 20, 0),
                new RowCountEstimator.TableDemand("d", false, 20, 1));
        var out = RowCountEstimator.estimate(p, demands, List.of(9, 1));
        int heavy = out.getRowsByTable().get("a");
        int light = out.getRowsByTable().get("d");
        assertTrue(heavy > light * 2,
                "权重 9:1 应体现在行数上（均分退化时两者会相等），实际 a=" + heavy + " d=" + light);
    }

    @Test
    void weightsAlignPerDemandNotPerEndpoint() {
        RowCountEstimator.StressParams p = params(1, 60, 100000);
        List<RowCountEstimator.TableDemand> demands = List.of(
                new RowCountEstimator.TableDemand("a", false, 20, 0),
                new RowCountEstimator.TableDemand("b", false, 20, 1));
        var out = RowCountEstimator.estimate(p, demands, List.of(1, 1));
        // 权重相等时，同一接口内的多张表也应得到相同行数
        assertEquals(out.getRowsByTable().get("a"), out.getRowsByTable().get("b"));
    }

    @Test
    void outOfRangeEndpointIndexFallsBackToZeroWeight() {
        // demand 指向不存在的接口下标：不应越界，按 0 权重处理
        RowCountEstimator.StressParams p = params(1, 60, 100000);
        List<RowCountEstimator.TableDemand> demands = List.of(
                new RowCountEstimator.TableDemand("a", false, 20, 0),
                new RowCountEstimator.TableDemand("b", false, 20, 5));
        var out = RowCountEstimator.estimate(p, demands, List.of(1));
        assertTrue(out.getRowsByTable().containsKey("b"), "越界下标不应丢弃该表");
    }

    @Test
    void demandDefaultsToEndpointZero() {
        assertEquals(0, new RowCountEstimator.TableDemand("a", false, 20).getEndpointIndex());
        assertEquals(0, new RowCountEstimator.TableDemand("a", false, 20, -3).getEndpointIndex());
    }
}
