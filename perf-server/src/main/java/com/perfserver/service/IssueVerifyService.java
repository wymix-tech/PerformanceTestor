package com.perfserver.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.entity.StressTask;
import com.perfserver.mapper.PerformanceIssueMapper;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.stress.StressTaskRequest;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

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
 * <p><b>异步执行</b>：重放压测由 {@link StressTaskService#createAndRun} 异步执行，等待其完成最长
 * {@code perf.verify.timeout-sec}（默认 300s）。若在本服务的 HTTP 调用线程上轮询等待，
 * 多人同时点"一键验证"会占满 Tomcat 工作线程导致整个服务无响应，故 {@link #verify}
 * 只做"校验快照 + 启动重放"（耗时在毫秒级），随后把轮询等待与达标判定交给独立的验证线程池，
 * 立即返回进行中结论；前端凭 {@link #verifyStatus} 轮询 {@code verify_json} 取最终结论。
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
    /** 验证线程池排队容量：超出的请求直接拒绝，不做无界堆积 */
    private static final int QUEUE_CAPACITY = 32;

    private final PerformanceIssueMapper issueMapper;
    private final StressTaskMapper taskMapper;
    private final StressTaskService stressTaskService;
    private final BaselineCompareService baselineCompareService;
    private final ObjectMapper objectMapper;
    private final int timeoutSec;
    /** 等待重放完成的线程池（轮询等待最长 timeoutSec，绝不能跑在 HTTP 工作线程上） */
    private final ThreadPoolExecutor executor;
    /** 正在验证的问题 ID：防止同一问题被重复发起重放压测 */
    private final Set<Long> runningIssueIds = ConcurrentHashMap.newKeySet();

    public IssueVerifyService(PerformanceIssueMapper issueMapper, StressTaskMapper taskMapper,
                              StressTaskService stressTaskService,
                              BaselineCompareService baselineCompareService, ObjectMapper objectMapper,
                              @Value("${perf.verify.timeout-sec:300}") int timeoutSec,
                              @Value("${perf.verify.max-concurrent:2}") int maxConcurrent) {
        this.issueMapper = issueMapper;
        this.taskMapper = taskMapper;
        this.stressTaskService = stressTaskService;
        this.baselineCompareService = baselineCompareService;
        this.objectMapper = objectMapper;
        this.timeoutSec = timeoutSec;
        int poolSize = Math.max(1, maxConcurrent);
        ThreadFactory factory = r -> {
            Thread t = new Thread(r, "issue-verify");
            t.setDaemon(true);
            return t;
        };
        this.executor = new ThreadPoolExecutor(poolSize, poolSize, 0L, TimeUnit.MILLISECONDS,
                new LinkedBlockingQueue<>(QUEUE_CAPACITY), factory);
    }

    @PreDestroy
    public void shutdown() {
        executor.shutdownNow();
    }

    /** 验证结果 */
    public static class VerifyResult {
        public long issueId;
        /** 无可用场景快照（问题未关联压测任务，或快照任务已删除） */
        public boolean noSnapshot;
        /** 重放压测失败（引擎异常/超时未完成） */
        public boolean replayFailed;
        /** 验证进行中（重放压测已发起，结论待落库——前端需轮询 {@code verifyStatus}） */
        public boolean running;
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

        public boolean isRunning() {
            return running;
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
     * 一键验证修复效果（异步）：校验快照 → 发起重放压测 → 立即返回进行中结论。
     *
     * <p>重放压测的等待与达标判定在验证线程池中完成，结论落库后由 {@link #verifyStatus} 读取，
     * HTTP 请求线程在毫秒级内释放，不再被最长 {@code perf.verify.timeout-sec} 的轮询占死。
     *
     * @throws IllegalArgumentException 问题不存在
     */
    public VerifyResult verify(long issueId) {
        PerformanceIssue issue = requireIssue(issueId);
        Snapshot resolved = resolveSnapshot(issue);
        if (resolved.error() != null) {
            return resolved.error();
        }
        StressTask snapshot = resolved.task();
        Long snapshotId = snapshot.getId();
        // 同一问题重复点击：直接返回已有状态，避免并发发起多份重放压测
        if (!runningIssueIds.add(issueId)) {
            return pending(issue, snapshotId, null, "已有一次验证在进行中，请勿重复发起");
        }

        // 提交判定任务前先检查验证线程池队列余量：若先发起重放、后因队列满被拒，
        // 已发起的重放压测无法收回——白白对目标服务打一轮压测，还留下无人等待判定的孤儿任务
        if (executor.getQueue().remainingCapacity() == 0) {
            runningIssueIds.remove(issueId);
            VerifyResult rejected = replayFailed(issue, snapshotId, null,
                    "当前等待验证的任务过多，请稍后再试");
            persist(issue, rejected);
            return rejected;
        }

        StressTask replay;
        try {
            replay = stressTaskService.createAndRun(toRequest(snapshot));
        } catch (Exception e) {
            runningIssueIds.remove(issueId);
            log.warn("verify replay start failed, issue={}: {}", issueId, e.getMessage());
            VerifyResult r = replayFailed(issue, snapshotId, null, "重放压测启动失败：" + e.getMessage());
            persist(issue, r);
            return r;
        }

        VerifyResult started = pending(issue, snapshotId, replay.getId(),
                "已发起重放压测（任务 #" + replay.getId() + "），完成后自动给出验证结论");
        // 进行中状态也要落库：进程重启或前端刷新后仍能查到"有验证在跑"
        persist(issue, started);
        try {
            executor.execute(() -> {
                try {
                    persist(issue, judge(issue, snapshot, replay));
                } catch (Exception e) {
                    log.warn("verify issue {} failed: {}", issueId, e.getMessage());
                    persist(issue, replayFailed(issue, snapshotId, replay.getId(),
                            "验证过程异常：" + e.getMessage()));
                } finally {
                    runningIssueIds.remove(issueId);
                }
            });
        } catch (RejectedExecutionException e) {
            // 前置的队列余量检查与提交之间存在并发窗口，仍可能被拒：此处兜底保证状态一致
            runningIssueIds.remove(issueId);
            VerifyResult r = replayFailed(issue, snapshotId, replay.getId(),
                    "当前等待验证的任务过多，请稍后再试");
            persist(issue, r);
            return r;
        }
        return started;
    }

    /**
     * 同步执行完整验证（发起重放 + 轮询等待 + 判定 + 落库）。
     *
     * <p>仅供单测直接驱动判定逻辑：线上入口必须是异步的 {@link #verify}，
     * 否则一次验证最长会占死 HTTP 工作线程 {@code perf.verify.timeout-sec} 秒。
     *
     * @throws IllegalArgumentException 问题不存在
     */
    VerifyResult runVerify(long issueId) {
        PerformanceIssue issue = requireIssue(issueId);
        Snapshot resolved = resolveSnapshot(issue);
        if (resolved.error() != null) {
            return resolved.error();
        }
        StressTask snapshot = resolved.task();
        StressTask replay;
        try {
            replay = stressTaskService.createAndRun(toRequest(snapshot));
        } catch (Exception e) {
            log.warn("verify replay start failed, issue={}: {}", issueId, e.getMessage());
            VerifyResult r = replayFailed(issue, snapshot.getId(), null,
                    "重放压测启动失败：" + e.getMessage());
            persist(issue, r);
            return r;
        }
        VerifyResult r = judge(issue, snapshot, replay);
        persist(issue, r);
        return r;
    }

    /**
     * 轮询等待重放任务完成 → 与快照基线 diff → 达标判定（<b>阻塞</b>，最长等待见
     * {@link #deadlineMs}）。
     *
     * <p>只允许运行在验证线程池（{@link #verify}）或单测线程（{@link #runVerify}）上；
     * 不落库，落库由调用方负责（进行中结论与最终结论需分别写入）。
     */
    private VerifyResult judge(PerformanceIssue issue, StressTask snapshot, StressTask replay) {
        StressTask finished = awaitFinished(replay.getId(), replay.getDurationSec());
        if (finished == null || !"finished".equals(finished.getStatus())) {
            String reason = finished == null ? "重放压测在 " + timeoutSec + " 秒内未完成"
                    : "重放压测失败：" + finished.getErrorMsg();
            return replayFailed(issue, snapshot.getId(), replay.getId(), reason);
        }

        BaselineCompareService.BaselineDiff diff =
                baselineCompareService.diff(finished.getId(), snapshot.getId());
        double p95Before = metric(snapshot, "p95Ms");
        double p95After = metric(finished, "p95Ms");
        double dropPct = p95Before <= 0 ? 0 : (p95Before - p95After) / p95Before * 100;

        VerifyResult r = new VerifyResult();
        r.issueId = issue.getId();
        r.snapshotTaskId = snapshot.getId();
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
        return r;
    }

    /**
     * 读取最近一次验证结论（含进行中状态）—— 一键验证为异步任务，前端据此轮询。
     *
     * @return {@code verify_json} 解析后的结论；尚无结论返回 null
     * @throws IllegalArgumentException 问题不存在
     */
    public Map<String, Object> verifyStatus(long issueId) {
        PerformanceIssue issue = requireIssue(issueId);
        String json = issue.getVerifyJson();
        if (json == null || json.isBlank()) {
            return null;
        }
        Map<String, Object> parsed;
        try {
            parsed = objectMapper.readValue(json, new TypeReference<Map<String, Object>>() { });
        } catch (Exception e) {
            log.warn("parse verify json failed, issue={}: {}", issueId, e.getMessage());
            return null;
        }
        // 服务重启会留下"进行中"的残留结论，靠内存态判断不可靠，一律按时间兜底：
        // 超出等待上限仍在 running，说明等待线程已随进程消失，需提示用户重新发起
        if (Boolean.TRUE.equals(parsed.get("running")) && staleRunning(issue.getVerifyTime())) {
            parsed.put("running", false);
            parsed.put("replayFailed", true);
            parsed.put("message", "上次验证已中断（服务重启或验证进程异常），请重新发起验证");
        }
        return parsed;
    }

    /** 进行中状态的最大存活时长（秒）：等待上限 + 收尾缓冲 */
    private long runningTtlSec() {
        return Math.max(MIN_WAIT_SEC, timeoutSec) + WAIT_BUFFER_SEC;
    }

    private boolean staleRunning(LocalDateTime verifyTime) {
        return verifyTime != null && verifyTime.plusSeconds(runningTtlSec()).isBefore(LocalDateTime.now());
    }

    /** 问题存在性校验（不存在直接抛错，由全局异常处理为 400） */
    private PerformanceIssue requireIssue(long issueId) {
        PerformanceIssue issue = issueMapper.findById(issueId);
        if (issue == null) {
            throw new IllegalArgumentException("问题不存在: " + issueId);
        }
        return issue;
    }

    /** 场景快照解析结果：{@code task} 为 null 时 {@code error} 为对应的无快照结论 */
    private record Snapshot(StressTask task, VerifyResult error) {
    }

    /** 解析场景快照；不可用时返回对应的 noSnapshot 结论（此时 {@link Snapshot#task()} 为 null） */
    private Snapshot resolveSnapshot(PerformanceIssue issue) {
        Long snapshotId = issue.getStressTaskId();
        if (snapshotId == null || snapshotId <= 0) {
            return new Snapshot(null, noSnapshot(issue.getId(), snapshotId, issue.getStatus(),
                    "该问题未关联压测场景快照，无法自动验证；可从压测任务创建问题以建立场景快照"));
        }
        StressTask snapshot = taskMapper.findById(snapshotId);
        if (snapshot == null) {
            return new Snapshot(null, noSnapshot(issue.getId(), snapshotId, issue.getStatus(),
                    "场景快照压测任务 #" + snapshotId + " 已不存在，无法自动验证"));
        }
        return new Snapshot(snapshot, null);
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

    /** 重放/判定失败结论（保持问题原状态） */
    private static VerifyResult replayFailed(PerformanceIssue issue, Long snapshotId, Long replayTaskId,
                                             String message) {
        VerifyResult r = new VerifyResult();
        r.issueId = issue.getId();
        r.snapshotTaskId = snapshotId;
        r.replayTaskId = replayTaskId;
        r.replayFailed = true;
        r.status = issue.getStatus();
        r.message = message;
        r.verifyTime = LocalDateTime.now();
        return r;
    }

    /** 进行中结论：落库后前端可凭 {@link #verifyStatus} 轮询，进程重启也能恢复展示 */
    private static VerifyResult pending(PerformanceIssue issue, Long snapshotId, Long replayTaskId,
                                        String message) {
        VerifyResult r = new VerifyResult();
        r.issueId = issue.getId();
        r.snapshotTaskId = snapshotId;
        r.replayTaskId = replayTaskId;
        r.running = true;
        r.status = issue.getStatus();
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
        node.put("running", r.running);
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
