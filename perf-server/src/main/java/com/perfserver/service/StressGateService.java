package com.perfserver.service;

import com.perfserver.entity.StressTask;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.notify.NotifyService;
import com.perfserver.stress.StressTaskRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;

/**
 * 持续性能守护门禁：把「触发压测 → 等待完成 → 与基线比对 → 判定 + 告警」串成
 * 一次调用即可拿到可判定结论的流水线能力。
 *
 * <p>设计要点：
 * <ul>
 *   <li><b>同步等待</b>：CI 无法处理「创建任务后自行轮询」的异步模型，
 *       故提供 {@link #runGate} 阻塞到任务完成（带超时），一次调用返回结论。</li>
 *   <li><b>复用既有阈值</b>：回归判定完全委托 {@link BaselineCompareService#diff}，
 *       不另起一套阈值常量，避免两处口径漂移。</li>
 *   <li><b>无基线可配置</b>：首次接入没有基线时，{@code failOnNoBaseline} 决定放行还是失败
 *       ——默认放行（避免新场景一接入就卡住流水线），但需要时可通过参数收紧。</li>
 *   <li><b>告警尽力而为</b>：通知失败仅记日志，不影响门禁结论。</li>
 * </ul>
 */
@Service
public class StressGateService {

    private static final Logger log = LoggerFactory.getLogger(StressGateService.class);

    private final StressTaskService taskService;
    private final StressTaskMapper taskMapper;
    private final BaselineCompareService baselineCompareService;
    private final NotifyService notifyService;

    /** 等待压测完成的默认超时（秒） */
    @Value("${perf.verify.timeout-sec:300}")
    private long defaultTimeoutSec = 300;

    /**
     * 等待超时的硬上限（秒）。
     *
     * <p>门禁接口是同步阻塞的（CI 需要一次调用拿到结论），会一直占用一个 HTTP 工作线程。
     * 校验允许的最大压测规模（时长 3600 + 预热 300 + 爬坡 600 + 10 档 × 600）约 3 小时，
     * 若不加限制，少量并发的门禁请求就能耗尽线程池拖垮整个平台。
     * 触顶时按上限等待，超时即判失败（任务仍在后台继续跑，可事后查报告）。
     */
    @Value("${perf.gate.max-timeout-sec:1800}")
    private long maxTimeoutSec = 1800;

    /**
     * 超时下限的收尾余量（秒）：覆盖调度排队、引擎启动、结果落库等固定开销，
     * 避免用户传入略小于压测时长的超时导致必然误判。
     */
    @Value("${perf.gate.timeout-overhead-sec:60}")
    private long timeoutOverheadSec = 60;

    /** 测试可调：覆盖超时余量（默认包内可见，避免生产代码暴露无谓 setter） */
    void setTimeoutOverheadSec(long timeoutOverheadSec) {
        this.timeoutOverheadSec = timeoutOverheadSec;
    }

    public StressGateService(StressTaskService taskService,
                             StressTaskMapper taskMapper,
                             BaselineCompareService baselineCompareService,
                             NotifyService notifyService) {
        this.taskService = taskService;
        this.taskMapper = taskMapper;
        this.baselineCompareService = baselineCompareService;
        this.notifyService = notifyService;
    }

    /**
     * 执行性能门禁：创建压测任务 → 等待完成 → 与基线比对 → 返回结论。
     *
     * @param request          压测请求（同普通压测任务参数）
     * @param baselineTaskId   基线任务 ID（{@code <=0} 自动取同应用同 URL 最近完成的任务）
     * @param timeoutSec       等待超时（秒；{@code <=0} 用默认配置）
     * @param failOnNoBaseline 无基线时是否判定为失败（默认 false = 放行并记为 noBaseline）
     * @param webhook          告警地址（为空不发送）
     * @return 门禁结论
     */
    public GateVerdict runGate(StressTaskRequest request, long baselineTaskId, long timeoutSec,
                               boolean failOnNoBaseline, String webhook) {
        long timeout = timeoutSec > 0 ? timeoutSec : defaultTimeoutSec;
        // 压测时长 + 调度与收尾余量：超时至少要覆盖一次完整压测，否则必然误判超时
        long minTimeout = estimateMinTimeout(request);
        if (timeout < minTimeout) {
            log.warn("门禁超时 {}s 小于预估最短耗时 {}s，自动放宽", timeout, minTimeout);
            timeout = minTimeout;
        }
        if (timeout > maxTimeoutSec) {
            log.warn("门禁超时 {}s 超过硬上限 {}s，按上限等待（超时即判失败，任务仍在后台继续）",
                    timeout, maxTimeoutSec);
            timeout = maxTimeoutSec;
        }

        StressTask task = taskService.createAndRun(request);
        GateVerdict verdict = new GateVerdict();
        verdict.taskId = task.getId();
        verdict.taskName = task.getTaskName();

        String status = awaitFinish(task.getId(), timeout);
        verdict.status = status;
        if (!"finished".equals(status)) {
            verdict.passed = false;
            verdict.summary = "压测未在 " + timeout + "s 内成功完成（状态：" + status + "）";
            notify(webhook, verdict);
            return verdict;
        }

        BaselineCompareService.BaselineDiff diff =
                baselineCompareService.diff(task.getId(), baselineTaskId);
        verdict.rows = diff.rows;
        verdict.noBaseline = diff.noBaseline;
        // noBaseline 时 baseline 可能为 null（任务缺失/结果为空），需判空
        if (diff.baseline != null && diff.baseline.getId() != null) {
            verdict.baselineTaskId = diff.baseline.getId();
        }
        verdict.regressed = diff.regressed;

        if (diff.noBaseline) {
            verdict.passed = !failOnNoBaseline;
            verdict.summary = failOnNoBaseline
                    ? "无可用基线，按 fail-on-no-baseline 策略判定为失败"
                    : "无可用基线（首次执行或历史任务缺失），本次结果已记录为后续基线，门禁放行";
        } else if (diff.regressed) {
            verdict.passed = false;
            verdict.summary = "性能回归：与基线任务 #" + verdict.baselineTaskId + " 相比，"
                    + describeRegressions(diff.rows);
        } else {
            verdict.passed = true;
            verdict.summary = "性能达标：与基线任务 #" + verdict.baselineTaskId + " 相比无回归";
        }
        notify(webhook, verdict);
        return verdict;
    }

    /** 轮询等待任务完成（分片睡眠，避免高频打库） */
    private String awaitFinish(long taskId, long timeoutSec) {
        long deadline = System.currentTimeMillis() + timeoutSec * 1000L;
        while (System.currentTimeMillis() < deadline) {
            StressTask t = taskMapper.findById(taskId);
            if (t == null) {
                return "missing";
            }
            String status = t.getStatus() == null ? "" : t.getStatus();
            if ("finished".equals(status) || "failed".equals(status)) {
                return status;
            }
            try {
                Thread.sleep(Math.min(1000, Math.max(100,
                        deadline - System.currentTimeMillis())));
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return "interrupted";
            }
        }
        return "timeout";
    }

    /** 预估一次压测的最短耗时（预热 + 正式 + 各档 + 收尾余量） */
    private long estimateMinTimeout(StressTaskRequest request) {
        long base = request.getDurationSec() + request.getWarmupSec() + request.getRampUpSec();
        if (request.getStages() != null) {
            for (StressTaskRequest.Stage s : request.getStages()) {
                base += s.getDurationSec();
            }
        }
        return base + timeoutOverheadSec;
    }

    /** 汇总回归明细（供结论文案与告警正文） */
    private static String describeRegressions(List<BaselineCompareService.DiffRow> rows) {
        List<String> parts = new ArrayList<>();
        for (BaselineCompareService.DiffRow r : rows) {
            if (r.regressed) {
                parts.add(r.label + " " + r.base + " → " + r.cur + "（" + r.deltaPct + "）");
            }
        }
        return parts.isEmpty() ? "错误率上升超阈值" : String.join("；", parts);
    }

    /** 发送门禁告警（仅失败时；webhook 为空则跳过） */
    private void notify(String webhook, GateVerdict verdict) {
        if (verdict.passed) {
            return;
        }
        try {
            notifyService.send(webhook, "性能门禁未通过 #" + verdict.taskId, verdict.summary);
        } catch (Exception e) {
            // 告警尽力而为：发送失败不得影响门禁结论
            log.warn("perf gate notify failed, taskId={}", verdict.taskId, e);
        }
    }

    /** 门禁结论（供流水线一次调用判定） */
    public static class GateVerdict {
        /** 任务 ID */
        public long taskId;
        /** 任务名（便于流水线日志定位） */
        public String taskName;
        /** 最终状态：finished / failed / timeout / missing / interrupted */
        public String status;
        /** 是否通过门禁 */
        public boolean passed;
        /** 是否因无基线而无法比对 */
        public boolean noBaseline;
        /** 是否命中回归阈值 */
        public boolean regressed;
        /** 基线任务 ID（0 = 无） */
        public long baselineTaskId;
        /** 指标比对明细 */
        public List<BaselineCompareService.DiffRow> rows = new ArrayList<>();
        /** 可直接用于通知与流水线日志的结论描述 */
        public String summary;
    }
}
