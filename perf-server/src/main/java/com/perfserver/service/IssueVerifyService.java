package com.perfserver.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.entity.StressTask;
import com.perfserver.mapper.PerformanceIssueMapper;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.stress.StressTaskRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

/**
 * 修复效果自动验证：以问题创建时的压测场景快照为基线，重放同场景压测并判定修复是否达标。
 *
 * <p>判定规则（同时满足才算达标）：
 * <ol>
 *   <li>P95 相对基线下降 ≥ {@value #P95_DROP_PCT}%；</li>
 *   <li>无回归（{@link BaselineCompareService.BaselineDiff#regressed} == false，
 *       即 QPS 降幅未超 10%、P95 涨幅未超 20%、错误率上升未超 5 个百分点）；</li>
 * </ol>
 * 达标则问题自动流转到 {@code resolved}，并写入 {@code verify_json} 结论。
 *
 * <p>重放压测复用 {@link StressTaskService#createAndRun}（异步执行），
 * 本服务轮询等待其完成（超时按 {@code perf.verify.timeout-sec} 配置，默认 300s）。
 */
@Service
public class IssueVerifyService {

    private static final Logger log = LoggerFactory.getLogger(IssueVerifyService.class);

    /** 达标阈值：P95 相对基线下降百分比 */
    public static final double P95_DROP_PCT = 30.0;
    /** 轮询间隔毫秒 */
    private static final long POLL_INTERVAL_MS = 500L;
    /** 轮询等待完成时，在压测时长之上额外预留的收尾时间（秒） */
    static final long WAIT_BUFFER_SEC = 60L;
    /** 等待预算下限（秒）：防止 timeoutSec / durationSec 误配为 0 导致正常任务被秒判超时 */
    static final long MIN_WAIT_SEC = 30L;

    private final PerformanceIssueMapper issueMapper;
    private final StressTaskMapper taskMapper;
    private final StressTaskService stressTaskService;
    private final BaselineCompareService baselineCompareService;
    private final ObjectMapper objectMapper;
    private final int timeoutSec;

    public IssueVerifyService(PerformanceIssueMapper issueMapper, StressTaskMapper taskMapper,
                              StressTaskService stressTaskService,
                              BaselineCompareService baselineCompareService, ObjectMapper objectMapper,
                              @org.springframework.beans.factory.annotation.Value("${perf.verify.timeout-sec:300}")
                              int timeoutSec) {
        this.issueMapper = issueMapper;
        this.taskMapper = taskMapper;
        this.stressTaskService = stressTaskService;
        this.baselineCompareService = baselineCompareService;
        this.objectMapper = objectMapper;
        this.timeoutSec = timeoutSec;
    }

    /** 验证结果 */
    public static class VerifyResult {
        public long issueId;
        /** 无可用场景快照（问题未关联压测任务，或快照任务已删除） */
        public boolean noSnapshot;
        /** 重放压测失败（引擎异常/超时未完成） */
        public boolean replayFailed;
        /** 达标（P95 降幅达标且无回归） */
        public boolean passed;
        public Long snapshotTaskId;
        public Long replayTaskId;
        public double p95Before;
        public double p95After;
        /** P95 下降百分比（正数表示下降） */
        public double p95DropPct;
        public double qpsBefore;
        public double qpsAfter;
        /** 存在任一指标回归 */
        public boolean regressed;
        /** 验证后的问题状态 */
        public String status;
        public String message;
        public LocalDateTime verifyTime;
        /** 基线对比明细（前端复用基线对比表格语义） */
        public List<Map<String, Object>> rows;

        public long getIssueId() {
            return issueId;
        }

        public boolean isNoSnapshot() {
            return noSnapshot;
        }

        public boolean isReplayFailed() {
            return replayFailed;
        }

        public boolean isPassed() {
            return passed;
        }

        public Long getSnapshotTaskId() {
            return snapshotTaskId;
        }

        public Long getReplayTaskId() {
            return replayTaskId;
        }

        public double getP95Before() {
            return p95Before;
        }

        public double getP95After() {
            return p95After;
        }

        public double getP95DropPct() {
            return p95DropPct;
        }

        public double getQpsBefore() {
            return qpsBefore;
        }

        public double getQpsAfter() {
            return qpsAfter;
        }

        public boolean isRegressed() {
            return regressed;
        }

        public String getStatus() {
            return status;
        }

        public String getMessage() {
            return message;
        }

        public LocalDateTime getVerifyTime() {
            return verifyTime;
        }

        public List<Map<String, Object>> getRows() {
            return rows;
        }
    }

    /**
     * 一键验证修复效果：重放快照场景压测 → 与快照基线 diff → 达标自动关闭问题。
     *
     * @throws IllegalArgumentException 问题不存在
     */
    public VerifyResult verify(long issueId) {
        PerformanceIssue issue = issueMapper.findById(issueId);
        if (issue == null) {
            throw new IllegalArgumentException("问题不存在: " + issueId);
        }
        Long snapshotId = issue.getStressTaskId();
        if (snapshotId == null || snapshotId <= 0) {
            return noSnapshot(issueId, snapshotId, issue.getStatus(), "该问题未关联压测场景快照，无法自动验证；"
                    + "可从压测任务创建问题以建立场景快照");
        }
        StressTask snapshot = taskMapper.findById(snapshotId);
        if (snapshot == null) {
            return noSnapshot(issueId, snapshotId, issue.getStatus(),
                    "场景快照压测任务 #" + snapshotId + " 已不存在，无法自动验证");
        }

        StressTask replay;
        try {
            replay = stressTaskService.createAndRun(toRequest(snapshot));
        } catch (Exception e) {
            log.warn("verify replay start failed, issue={}: {}", issueId, e.getMessage());
            VerifyResult r = new VerifyResult();
            r.issueId = issueId;
            r.snapshotTaskId = snapshotId;
            r.replayFailed = true;
            r.status = issue.getStatus();
            r.message = "重放压测启动失败：" + e.getMessage();
            r.verifyTime = LocalDateTime.now();
            persist(issue, r);
            return r;
        }

        StressTask finished = awaitFinished(replay.getId(), replay.getDurationSec());
        if (finished == null || !"finished".equals(finished.getStatus())) {
            String reason = finished == null ? "重放压测在 " + timeoutSec + " 秒内未完成"
                    : "重放压测失败：" + finished.getErrorMsg();
            VerifyResult r = new VerifyResult();
            r.issueId = issueId;
            r.snapshotTaskId = snapshotId;
            r.replayTaskId = replay.getId();
            r.replayFailed = true;
            r.status = issue.getStatus();
            r.message = reason;
            r.verifyTime = LocalDateTime.now();
            persist(issue, r);
            return r;
        }

        BaselineCompareService.BaselineDiff diff =
                baselineCompareService.diff(finished.getId(), snapshotId);
        double p95Before = metric(snapshot, "p95Ms");
        double p95After = metric(finished, "p95Ms");
        double dropPct = p95Before <= 0 ? 0 : (p95Before - p95After) / p95Before * 100;

        VerifyResult r = new VerifyResult();
        r.issueId = issueId;
        r.snapshotTaskId = snapshotId;
        r.replayTaskId = finished.getId();
        r.p95Before = p95Before;
        r.p95After = p95After;
        r.p95DropPct = round(dropPct);
        r.qpsBefore = metric(snapshot, "qps");
        r.qpsAfter = metric(finished, "qps");
        r.regressed = diff.regressed;
        r.rows = toRows(diff);
        r.verifyTime = LocalDateTime.now();
        r.passed = dropPct >= P95_DROP_PCT && !diff.regressed;
        r.status = r.passed ? "resolved" : issue.getStatus();
        r.message = r.passed
                ? "验证通过：P95 由 " + fmt(p95Before) + "ms 降至 " + fmt(p95After) + "ms（下降 "
                        + fmt(r.p95DropPct) + "%），无指标回归，问题已自动关闭"
                : buildNotPassedMessage(dropPct, diff, p95Before, p95After);
        persist(issue, r);
        return r;
    }

    /**
     * 轮询等待压测任务完成（finished/failed）；超时返回 null。
     *
     * <p>等待上限见 {@link #deadlineMs}，到达上限任务仍未终态时返回 null（调用方记为重放失败）。
     */
    private StressTask awaitFinished(long taskId, Integer durationSec) {
        long deadline = deadlineMs(System.currentTimeMillis(), timeoutSec, durationSec);
        for (;;) {
            StressTask t = taskMapper.findById(taskId);
            if (t != null && ("finished".equals(t.getStatus()) || "failed".equals(t.getStatus()))) {
                return t;
            }
            if (System.currentTimeMillis() >= deadline) {
                return null;
            }
            try {
                Thread.sleep(POLL_INTERVAL_MS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return null;
            }
        }
    }

    /**
     * 轮询等待的绝对截止时间戳（毫秒）。
     *
     * <p>取「配置超时」与「压测时长 + 收尾缓冲」两者的较小值：短任务不必空等满
     * {@code perf.verify.timeout-sec}；任务时长本身超过超时配置时以配置为准，避免无限等待。
     * 两侧均有 {@value #MIN_WAIT_SEC} 秒下限，防止误配成 0 导致正常任务被秒判超时。
     *
     * @param nowMs      当前时间戳（注入而非内部取 {@code currentTimeMillis}，便于单测断言，无需真实等待）
     * @param timeoutSec 配置的等待上限（秒）
     * @param durationSec 重放压测时长（秒，为空时按 {@value #WAIT_BUFFER_SEC} 秒兜底）
     */
    static long deadlineMs(long nowMs, int timeoutSec, Integer durationSec) {
        long budgetMs = Math.max(MIN_WAIT_SEC, timeoutSec) * 1000L;
        long warmupBudgetMs = Math.max(MIN_WAIT_SEC, durationSec == null ? WAIT_BUFFER_SEC
                : durationSec + WAIT_BUFFER_SEC) * 1000L;
        return nowMs + Math.min(budgetMs, warmupBudgetMs);
    }

    /** 快照任务 → 重放请求（同场景：URL/方法/并发/时长/限流/应用一致） */
    private static StressTaskRequest toRequest(StressTask snapshot) {
        StressTaskRequest req = new StressTaskRequest();
        req.setTaskName("修复验证重放 #" + snapshot.getId());
        req.setEngineType(snapshot.getEngineType() == null || snapshot.getEngineType().isBlank()
                ? "http" : snapshot.getEngineType());
        req.setTargetUrl(snapshot.getTargetUrl());
        req.setAppName(snapshot.getAppName());
        req.setMethod(snapshot.getMethod() == null || snapshot.getMethod().isBlank()
                ? "GET" : snapshot.getMethod());
        req.setConcurrency(snapshot.getConcurrency() == null ? 1 : snapshot.getConcurrency());
        req.setDurationSec(snapshot.getDurationSec() == null ? 60 : snapshot.getDurationSec());
        req.setQpsLimit(snapshot.getQpsLimit() == null ? 0 : snapshot.getQpsLimit());
        return req;
    }

    /** 读取压测结果 JSON 中的指标（缺省 0） */
    private double metric(StressTask task, String field) {
        try {
            JsonNode node = objectMapper.readTree(task.getResultJson() == null ? "{}" : task.getResultJson());
            return node.path(field).asDouble(0);
        } catch (Exception e) {
            log.warn("parse stress result failed, taskId={}", task.getId());
            return 0;
        }
    }

    /** 基线 diff 行 → 前端表格结构 */
    private static List<Map<String, Object>> toRows(BaselineCompareService.BaselineDiff diff) {
        if (diff.rows == null) {
            return List.of();
        }
        List<Map<String, Object>> rows = new java.util.ArrayList<>(diff.rows.size());
        for (BaselineCompareService.DiffRow r : diff.rows) {
            rows.add(Map.of("label", r.label, "base", r.base, "cur", r.cur,
                    "deltaPct", r.deltaPct, "improved", r.improved, "regressed", r.regressed));
        }
        return rows;
    }

    private static String buildNotPassedMessage(double dropPct, BaselineCompareService.BaselineDiff diff,
                                                double p95Before, double p95After) {
        StringBuilder sb = new StringBuilder("验证未达标：P95 由 ").append(fmt(p95Before))
                .append("ms 变为 ").append(fmt(p95After)).append("ms（下降 ").append(fmt(dropPct))
                .append("%，需 ≥ ").append((int) P95_DROP_PCT).append("%）");
        if (diff.regressed) {
            sb.append("；且存在指标回归：");
            if (diff.rows != null) {
                diff.rows.stream().filter(r -> r.regressed)
                        .forEach(r -> sb.append(r.label).append(" ").append(r.deltaPct).append("、"));
            }
            sb.setLength(sb.length() - 1);
        }
        sb.append("。问题保持原状态，建议复查修复是否生效。");
        return sb.toString();
    }

    private static VerifyResult noSnapshot(long issueId, Long snapshotId, String currentStatus, String message) {
        VerifyResult r = new VerifyResult();
        r.issueId = issueId;
        r.snapshotTaskId = snapshotId;
        r.noSnapshot = true;
        r.status = currentStatus;
        r.message = message;
        r.verifyTime = LocalDateTime.now();
        return r;
    }

    /** 落库：写 verify_json / verify_time，达标时置 resolved */
    private void persist(PerformanceIssue issue, VerifyResult r) {
        // 无快照属于"无法验证"，不写入结论（避免污染问题状态与验证时间）
        if (r.noSnapshot) {
            return;
        }
        try {
            issueMapper.updateVerify(issue.getId(), toJson(r), r.verifyTime, r.status);
            issue.setStatus(r.status);
            issue.setVerifyTime(r.verifyTime);
        } catch (Exception e) {
            log.warn("persist verify result failed, issue={}: {}", issue.getId(), e.getMessage());
        }
    }

    /** 结论 JSON（前端直接展示，字段与前端表格对齐） */
    private String toJson(VerifyResult r) {
        ObjectNode node = objectMapper.createObjectNode();
        node.put("issueId", r.issueId);
        node.put("passed", r.passed);
        node.put("replayFailed", r.replayFailed);
        node.put("snapshotTaskId", r.snapshotTaskId == null ? 0 : r.snapshotTaskId);
        node.put("replayTaskId", r.replayTaskId == null ? 0 : r.replayTaskId);
        node.put("p95Before", r.p95Before);
        node.put("p95After", r.p95After);
        node.put("p95DropPct", r.p95DropPct);
        node.put("qpsBefore", r.qpsBefore);
        node.put("qpsAfter", r.qpsAfter);
        node.put("regressed", r.regressed);
        node.put("status", r.status);
        node.put("message", r.message);
        node.put("verifyTime", r.verifyTime == null ? "" : r.verifyTime.toString());
        ArrayNode arr = node.putArray("rows");
        if (r.rows != null) {
            for (Map<String, Object> row : r.rows) {
                ObjectNode n = arr.addObject();
                row.forEach((k, v) -> {
                    if (v instanceof Boolean b) {
                        n.put(k, b);
                    } else if (v instanceof Number num) {
                        n.put(k, num.doubleValue());
                    } else {
                        n.put(k, v == null ? "" : String.valueOf(v));
                    }
                });
            }
        }
        try {
            return objectMapper.writeValueAsString(node);
        } catch (Exception e) {
            return "{}";
        }
    }

    private static double round(double v) {
        return Math.round(v * 10.0) / 10.0;
    }

    private static String fmt(double v) {
        return v == Math.floor(v) ? String.valueOf((long) v) : String.format("%.1f", v);
    }
}
