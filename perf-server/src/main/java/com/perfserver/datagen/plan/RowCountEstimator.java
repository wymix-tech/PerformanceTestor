package com.perfserver.datagen.plan;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 行数推导器：按压测参数（并发 × 时长 × QPS）与分页大小推导各表行数，替代固定默认值。
 *
 * <p>核心公式：</p>
 * <pre>
 * peakQps       = qpsLimit &gt; 0 ? qpsLimit : concurrency * (1000 / assumedRtMs)
 * totalRequests = Σ stages(peakQps(stage) * durationSec(stage))，无分档用单档，clamp[1, 1e9]
 * 场景模式       = stepRequests_i = totalRequests * w_i / Σw
 * READ  主表     = clamp(ceil(stepRequests * HIT_RATIO),  1000, 200000)，且 &gt;= pageSize * 20
 * WRITE 主表     = clamp(ceil(stepRequests * WRITE_RATIO),1000, 200000)
 * 父表（被 ref） = clamp(ceil(子表 rows / AVG_CHILDREN),   100,  200000)
 * 合并           = 同表取 max，谓词值集取 union
 * 全局           = Σrows 超过 MAX_TOTAL_ROWS 时按比例缩减并写 warnings
 * </pre>
 *
 * <p>同表取 max 而非求和：同一张表被多个场景步骤命中时，求和会把行数放大到失真。
 * 取 max 保证「每个步骤查这张表时都够用」，同时不被放大。</p>
 *
 * <p>无状态、纯计算，便于单测覆盖各分支与上限保护。</p>
 */
public final class RowCountEstimator {

    /** 总行数上限（与 DataGenService.MAX_ROWS 一致） */
    public static final int MAX_TOTAL_ROWS = 1_000_000;
    /** 单表行数上限 */
    public static final int MAX_TABLE_ROWS = 200_000;
    /** 读接口：请求量中需要"能被查到"的数据占比 */
    public static final double HIT_RATIO = 0.1;
    /** 写接口：请求量直接对应写入行数 */
    public static final double WRITE_RATIO = 1.0;
    /** 父表：平均每个父记录拥有的子记录数 */
    public static final double AVG_CHILDREN = 10.0;
    /** 分页兜底：主表行数至少为 pageSize 的若干倍，保证翻页能翻到底 */
    public static final int PAGE_MULTIPLIER = 20;
    /** 缺省分页大小 */
    public static final int DEFAULT_PAGE_SIZE = 20;
    /** 主表行数下限 */
    public static final int MIN_MAIN_ROWS = 1_000;
    /** 父表行数下限 */
    public static final int MIN_PARENT_ROWS = 100;

    private RowCountEstimator() {
    }

    /** 压测参数（单档；分档场景由 stages 承载） */
    public static final class StressParams {
        private int concurrency = 1;
        private int durationSec = 60;
        private double qpsLimit;
        private double assumedRtMs = RtBaselineResolver.DEFAULT_RT_MS;
        private List<Stage> stages = List.of();

        public int getConcurrency() {
            return concurrency;
        }

        public void setConcurrency(int concurrency) {
            this.concurrency = Math.max(1, concurrency);
        }

        public int getDurationSec() {
            return durationSec;
        }

        public void setDurationSec(int durationSec) {
            this.durationSec = Math.max(1, durationSec);
        }

        public double getQpsLimit() {
            return qpsLimit;
        }

        public void setQpsLimit(double qpsLimit) {
            this.qpsLimit = qpsLimit;
        }

        public double getAssumedRtMs() {
            return assumedRtMs;
        }

        public void setAssumedRtMs(double assumedRtMs) {
            this.assumedRtMs = assumedRtMs;
        }

        public List<Stage> getStages() {
            return stages == null ? List.of() : stages;
        }

        public void setStages(List<Stage> stages) {
            this.stages = stages == null ? List.of() : stages;
        }
    }

    /** 并发分档 */
    public static final class Stage {
        private int concurrency = 1;
        private int durationSec = 60;

        public int getConcurrency() {
            return concurrency;
        }

        public void setConcurrency(int concurrency) {
            this.concurrency = Math.max(1, concurrency);
        }

        public int getDurationSec() {
            return durationSec;
        }

        public void setDurationSec(int durationSec) {
            this.durationSec = Math.max(1, durationSec);
        }
    }

    /** 单表行数请求：表名 + 读写类型 + 分页大小 */
    public static final class TableDemand {
        private final String table;
        private final boolean write;
        private final int pageSize;

        public TableDemand(String table, boolean write, int pageSize) {
            this.table = table == null ? "" : table.toLowerCase(Locale.ROOT);
            this.write = write;
            this.pageSize = pageSize > 0 ? pageSize : DEFAULT_PAGE_SIZE;
        }

        public String getTable() {
            return table;
        }

        public boolean isWrite() {
            return write;
        }

        public int getPageSize() {
            return pageSize;
        }
    }

    /** 推导结果 */
    public static final class Estimate {
        private final Map<String, Integer> rowsByTable = new LinkedHashMap<>();
        private final List<String> warnings = new ArrayList<>();
        private long totalRequests;
        private double peakQps;

        public Map<String, Integer> getRowsByTable() {
            return rowsByTable;
        }

        public List<String> getWarnings() {
            return warnings;
        }

        public long getTotalRequests() {
            return totalRequests;
        }

        public double getPeakQps() {
            return peakQps;
        }
    }

    /**
     * 推导各表行数。
     *
     * @param params  压测参数
     * @param demands 表需求（按场景步骤权重已折算到 stepRequests）
     * @param steps   场景步骤权重（空或单元素时按单接口处理）
     * @return 推导结果（含告警）
     */
    public static Estimate estimate(StressParams params, List<TableDemand> demands, List<Integer> weights) {
        Estimate out = new Estimate();
        if (params == null || demands == null || demands.isEmpty()) {
            return out;
        }
        out.peakQps = peakQps(params);
        out.totalRequests = totalRequests(params, out.peakQps);

        List<Double> stepRequests = splitByWeight(out.totalRequests, weights, demands.size());
        for (int i = 0; i < demands.size(); i++) {
            TableDemand d = demands.get(i);
            double req = stepRequests.get(i);
            int rows = d.isWrite()
                    ? mainRows(req * WRITE_RATIO, d.getPageSize())
                    : mainRows(req * HIT_RATIO, d.getPageSize());
            // 同表取 max：避免多步骤命中同一张表时行数被放大
            int prev = out.rowsByTable.getOrDefault(d.getTable(), 0);
            out.rowsByTable.put(d.getTable(), Math.max(prev, rows));
        }
        shrinkToLimit(out);
        return out;
    }

    /** 单接口便捷入口（权重缺省为 1） */
    public static Estimate estimate(StressParams params, List<TableDemand> demands) {
        return estimate(params, demands, null);
    }

    /** 父表行数：子表行数 / 平均子记录数，受上下限保护 */
    public static int parentRows(int childRows) {
        long v = Math.round(childRows / AVG_CHILDREN);
        return (int) Math.max(MIN_PARENT_ROWS, Math.min(MAX_TABLE_ROWS, v));
    }

    /** 峰值 QPS：显式 qpsLimit 优先，否则按并发与 RT 估算 */
    static double peakQps(StressParams params) {
        return peakQps(params, params.getConcurrency());
    }

    static double peakQps(StressParams params, int concurrency) {
        if (params.getQpsLimit() > 0) {
            return params.getQpsLimit();
        }
        double rt = params.getAssumedRtMs() > 0 ? params.getAssumedRtMs() : RtBaselineResolver.DEFAULT_RT_MS;
        return Math.max(0.01, concurrency) * (1000.0 / rt);
    }

    /** 总请求量：分档求和，无分档按单档 */
    static long totalRequests(StressParams params, double peakQps) {
        List<Stage> stages = params.getStages();
        if (stages == null || stages.isEmpty()) {
            return clampRequests(peakQps * params.getDurationSec());
        }
        double sum = 0;
        for (Stage s : stages) {
            sum += peakQps(params, s.getConcurrency()) * s.getDurationSec();
        }
        return clampRequests(sum);
    }

    /** 按步骤权重分摊请求量；权重缺失/全零时均分 */
    static List<Double> splitByWeight(long totalRequests, List<Integer> weights, int count) {
        List<Double> out = new ArrayList<>();
        if (count <= 0) {
            return out;
        }
        boolean usable = weights != null && weights.size() == count;
        double sum = 0;
        if (usable) {
            for (Integer w : weights) {
                sum += w == null || w < 0 ? 0 : w;
            }
        }
        if (!usable || sum <= 0) {
            for (int i = 0; i < count; i++) {
                out.add((double) totalRequests / count);
            }
            return out;
        }
        for (int i = 0; i < count; i++) {
            Integer w = weights.get(i);
            out.add(totalRequests * (w == null || w < 0 ? 0 : w) / sum);
        }
        return out;
    }

    /** 主表行数：区间保护 + 分页下限 */
    static int mainRows(double rawRows, int pageSize) {
        long v = (long) Math.ceil(rawRows);
        int pageFloor = Math.max(1, pageSize) * PAGE_MULTIPLIER;
        return (int) Math.max(Math.max(MIN_MAIN_ROWS, pageFloor), Math.min(MAX_TABLE_ROWS, v));
    }

    /** 全局上限：按比例缩减并告警（不静默截断） */
    private static void shrinkToLimit(Estimate out) {
        long total = out.rowsByTable.values().stream().mapToLong(Integer::longValue).sum();
        if (total <= MAX_TOTAL_ROWS) {
            return;
        }
        double scale = (double) MAX_TOTAL_ROWS / total;
        long reduced = 0;
        for (Map.Entry<String, Integer> e : new ArrayList<>(out.rowsByTable.entrySet())) {
            int v = (int) Math.max(1, Math.floor(e.getValue() * scale));
            out.rowsByTable.put(e.getKey(), v);
            reduced += v;
        }
        out.warnings.add(String.format(Locale.ROOT,
                "方案总行数 %d 超过上限 %d，已按比例缩减至 %d 行", total, MAX_TOTAL_ROWS, reduced));
    }

    private static long clampRequests(double v) {
        if (Double.isNaN(v) || Double.isInfinite(v)) {
            return 1;
        }
        return (long) Math.max(1, Math.min(1e9, v));
    }
}
