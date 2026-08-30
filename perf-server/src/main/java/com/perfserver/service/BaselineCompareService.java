package com.perfserver.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.StressTask;
import com.perfserver.mapper.StressTaskMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;

/**
 * 版本基线对比服务：同场景（同应用 + 同 URL）跨版本压测指标 diff，识别性能回归。
 *
 * <p>回归判定阈值：QPS 下降超 10%、P95 上升超 20%、错误率上升超 5 个百分点
 * （任一命中即 regressed=true，配合 NotifyService 产生回归门禁告警）。
 */
@Service
public class BaselineCompareService {

    private static final Logger log = LoggerFactory.getLogger(BaselineCompareService.class);

    /** 回归阈值（public：StressScheduler 告警文案引用） */
    public static final double QPS_DROP_PCT = 10.0;
    public static final double P95_RISE_PCT = 20.0;
    public static final double ERROR_RATE_RISE_PCT = 5.0;

    private final StressTaskMapper taskMapper;
    private final ObjectMapper objectMapper;

    public BaselineCompareService(StressTaskMapper taskMapper, ObjectMapper objectMapper) {
        this.taskMapper = taskMapper;
        this.objectMapper = objectMapper;
    }

    /** 对比行 */
    public static class DiffRow {
        public final String label;
        public final String base;
        public final String cur;
        public final String deltaPct;
        public final boolean improved;
        public final boolean regressed;

        public DiffRow(String label, String base, String cur, String deltaPct, boolean improved, boolean regressed) {
            this.label = label;
            this.base = base;
            this.cur = cur;
            this.deltaPct = deltaPct;
            this.improved = improved;
            this.regressed = regressed;
        }
    }

    /** 对比结果 */
    public static class BaselineDiff {
        public StressTask baseline;
        public StressTask current;
        public List<DiffRow> rows = new ArrayList<>();
        /** 任一指标命中回归阈值 */
        public boolean regressed;
        /** 无可用基线（首次执行/历史任务缺失） */
        public boolean noBaseline;
    }

    /**
     * 对比当前任务与基线任务。
     *
     * @param taskId         当前任务
     * @param baselineTaskId 基线任务（&lt;=0 时自动取同应用同 URL 的最近完成任务）
     */
    public BaselineDiff diff(long taskId, long baselineTaskId) {
        BaselineDiff result = new BaselineDiff();
        StressTask current = taskMapper.findById(taskId);
        if (current == null) {
            result.noBaseline = true;
            return result;
        }
        result.current = current;
        StressTask baseline = baselineTaskId > 0
                ? taskMapper.findById(baselineTaskId)
                : autoBaseline(current);
        if (baseline == null || baseline.getId() == null || baseline.getId().equals(current.getId())) {
            result.noBaseline = true;
            return result;
        }
        result.baseline = baseline;

        JsonNode base = parseResult(baseline.getResultJson());
        JsonNode cur = parseResult(current.getResultJson());
        if (base == null || cur == null) {
            result.noBaseline = true;
            return result;
        }

        // QPS：越高越好
        double qpsB = base.path("qps").asDouble(0);
        double qpsC = cur.path("qps").asDouble(0);
        result.rows.add(row("QPS", qpsB, qpsC, true));
        // RT 分位：越低越好
        result.rows.add(row("P95 (ms)", base.path("p95Ms").asDouble(0), cur.path("p95Ms").asDouble(0), false));
        result.rows.add(row("P99 (ms)", base.path("p99Ms").asDouble(0), cur.path("p99Ms").asDouble(0), false));
        // 错误率：越低越好（百分点差值判定）
        double errB = base.path("errorRate").asDouble(0);
        double errC = cur.path("errorRate").asDouble(0);
        result.rows.add(row("错误率 (%)", errB, errC, false));

        result.regressed = result.rows.stream().anyMatch(r -> r.regressed)
                || (errC - errB) > ERROR_RATE_RISE_PCT;
        return result;
    }

    /** 自动基线：同应用同 URL、完成时间早于当前任务创建时间的最近完成任务 */
    private StressTask autoBaseline(StressTask current) {
        List<StressTask> finished = taskMapper.query("finished", 200);
        StressTask best = null;
        for (StressTask t : finished) {
            if (t.getId() == null || t.getId().equals(current.getId())) {
                continue;
            }
            boolean sameApp = (current.getAppName() == null || current.getAppName().isBlank()
                    || current.getAppName().equals(t.getAppName()));
            boolean sameUrl = current.getTargetUrl() != null && current.getTargetUrl().equals(t.getTargetUrl());
            boolean earlier = t.getFinishTime() != null
                    && (current.getCreateTime() == null || !t.getFinishTime().isAfter(current.getCreateTime()));
            boolean newer = best == null || (best.getFinishTime() != null && t.getFinishTime() != null
                    && t.getFinishTime().isAfter(best.getFinishTime()));
            if (sameApp && sameUrl && earlier && newer) {
                best = t;
            }
        }
        return best;
    }

    /** 单指标对比行（higherBetter：true = 值越高越好） */
    private static DiffRow row(String label, double base, double cur, boolean higherBetter) {
        String delta = deltaPct(cur, base);
        boolean improved = false;
        boolean regressed = false;
        if (!delta.isEmpty() && base != 0) {
            double pct = (cur - base) / Math.abs(base) * 100;
            improved = higherBetter ? pct > 0 : pct < 0;
            if (higherBetter) {
                regressed = -pct > QPS_DROP_PCT;
            } else if (label.startsWith("P95")) {
                regressed = pct > P95_RISE_PCT;
            }
        }
        return new DiffRow(label, fmt(base), fmt(cur), delta, improved, regressed);
    }

    private static String deltaPct(double cur, double base) {
        if (base == 0) {
            return "";
        }
        double v = (cur - base) / Math.abs(base) * 100;
        return (v > 0 ? "+" : "") + String.format("%.1f", v) + "%";
    }

    private static String fmt(double v) {
        return v == Math.floor(v) ? String.valueOf((long) v) : String.format("%.2f", v);
    }

    private JsonNode parseResult(String resultJson) {
        try {
            if (resultJson == null || resultJson.isBlank()) {
                return null;
            }
            return objectMapper.readTree(resultJson);
        } catch (Exception e) {
            log.warn("parse task resultJson failed", e);
            return null;
        }
    }
}
