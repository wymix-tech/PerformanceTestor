package com.perfserver.stress;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.StressSchedule;
import com.perfserver.entity.StressTask;
import com.perfserver.mapper.StressScheduleMapper;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.notify.NotifyService;
import com.perfserver.service.BaselineCompareService;
import com.perfserver.service.StressTaskService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 定时压测调度器：30s 扫描到期调度 → 复用 StressTaskService 触发压测 →
 * 任务完成后做版本基线对比 → 回归超阈值时经 webhook 告警（性能回归门禁）。
 *
 * <p>防重复触发：updateRunState 以 next_run_time 旧值为条件原子推进（CAS）。
 */
@Component
public class StressScheduler {

    private static final Logger log = LoggerFactory.getLogger(StressScheduler.class);

    private final StressScheduleMapper scheduleMapper;
    private final StressTaskMapper taskMapper;
    private final StressTaskService taskService;
    private final BaselineCompareService baselineService;
    private final NotifyService notifyService;
    private final ObjectMapper objectMapper;

    public StressScheduler(StressScheduleMapper scheduleMapper,
                           StressTaskMapper taskMapper,
                           StressTaskService taskService,
                           BaselineCompareService baselineService,
                           NotifyService notifyService,
                           ObjectMapper objectMapper) {
        this.scheduleMapper = scheduleMapper;
        this.taskMapper = taskMapper;
        this.taskService = taskService;
        this.baselineService = baselineService;
        this.notifyService = notifyService;
        this.objectMapper = objectMapper;
    }

    /** 调度主循环：触发到期任务 + 收尾已完成任务（基线对比与告警） */
    @Scheduled(fixedDelay = 30_000, initialDelay = 30_000)
    public void tick() {
        try {
            triggerDue();
        } catch (Exception e) {
            log.error("stress scheduler trigger failed", e);
        }
        try {
            settleFinished();
        } catch (Exception e) {
            log.error("stress scheduler settle failed", e);
        }
    }

    /** 触发到期调度（CAS 推进 next_run_time 防并发重复） */
    private void triggerDue() {
        List<StressSchedule> due = scheduleMapper.findDue(LocalDateTime.now(), 10);
        for (StressSchedule s : due) {
            try {
                StressTaskRequest request = objectMapper.readValue(s.getTaskJson(), StressTaskRequest.class);
                // 定时任务标记归属（任务名前缀便于追溯）
                if (request.getTaskName() == null || request.getTaskName().isBlank()) {
                    request.setTaskName("定时-" + s.getScheduleName());
                } else {
                    request.setTaskName("定时-" + request.getTaskName());
                }
                StressTask task = taskService.createAndRun(request);
                LocalDateTime now = LocalDateTime.now();
                LocalDateTime next = now.plusMinutes(Math.max(1, s.getIntervalMinutes()));
                // CAS：仅当 next_run_time 仍为上次值时推进（updateRunState 以旧值为条件）
                int updated = scheduleMapper.updateRunState(s.getId(),
                        task.getId(), s.getNextRunTime(), next);
                if (updated == 0) {
                    log.warn("stress schedule {} concurrently triggered, skipped", s.getId());
                } else {
                    log.info("stress schedule {} triggered task {}", s.getId(), task.getId());
                }
            } catch (Exception e) {
                log.error("stress schedule {} trigger failed", s.getId(), e);
                // 失败也要推进 next_run_time，否则每 30s 重试失败任务
                LocalDateTime now = LocalDateTime.now();
                scheduleMapper.updateRunState(s.getId(), 0, s.getNextRunTime(),
                        now.plusMinutes(Math.max(1, s.getIntervalMinutes())));
            }
        }
    }

    /** 收尾上次触发的已完成任务：基线对比 + 回归告警 */
    private void settleFinished() {
        List<StressSchedule> all = scheduleMapper.listAll();
        for (StressSchedule s : all) {
            Long lastTaskId = s.getLastTaskId();
            if (lastTaskId == null || lastTaskId <= 0) {
                continue;
            }
            StressTask task = taskMapper.findById(lastTaskId);
            if (task == null) {
                scheduleMapper.clearLastTask(s.getId());
                continue;
            }
            String status = task.getStatus();
            if (!"finished".equals(status) && !"failed".equals(status)) {
                continue; // 仍在执行
            }
            try {
                if ("finished".equals(status)) {
                    settle(s, task);
                } else {
                    // 压测失败直接告警（不对比）
                    notifyService.send(s.getWebhookUrl(), "[PerformanceTestor] 定时压测失败",
                            "调度「" + s.getScheduleName() + "」任务 #" + task.getId() + " 执行失败："
                                    + (task.getErrorMsg() == null ? "未知原因" : task.getErrorMsg()));
                }
            } finally {
                scheduleMapper.clearLastTask(s.getId());
            }
        }
    }

    /** 完成收尾：基线对比 + 回归判定 + 告警 */
    private void settle(StressSchedule s, StressTask task) {
        long baselineId = s.getBaselineTaskId() == null ? 0 : s.getBaselineTaskId();
        BaselineCompareService.BaselineDiff diff = baselineService.diff(task.getId(), baselineId);
        if (diff.noBaseline) {
            log.info("stress schedule {} task {} finished, no baseline available", s.getId(), task.getId());
            return;
        }
        StringBuilder text = new StringBuilder();
        text.append("当前: 任务#").append(task.getId())
                .append(task.getAppVersion() != null && !task.getAppVersion().isEmpty()
                        ? " v" + task.getAppVersion() : "")
                .append(" · 基线: 任务#").append(diff.baseline.getId())
                .append(diff.baseline.getAppVersion() != null && !diff.baseline.getAppVersion().isEmpty()
                        ? " v" + diff.baseline.getAppVersion() : " 未知版本")
                .append('\n');
        for (BaselineCompareService.DiffRow row : diff.rows) {
            text.append(row.label).append(": ").append(row.base).append(" → ").append(row.cur)
                    .append("（").append(row.deltaPct.isEmpty() ? "—" : row.deltaPct).append("）\n");
        }
        if (diff.regressed) {
            text.append("\n⚠ 检测到性能回归（QPS↓>").append((long) BaselineCompareService.QPS_DROP_PCT)
                    .append("% / P95↑>").append((long) BaselineCompareService.P95_RISE_PCT)
                    .append("% / 错误率↑>").append((long) BaselineCompareService.ERROR_RATE_RISE_PCT)
                    .append("pct 任一命中）");
            notifyService.send(s.getWebhookUrl(), "[PerformanceTestor] 性能回归告警", text.toString());
            log.warn("stress schedule {} regression detected, task={}", s.getId(), task.getId());
        } else {
            log.info("stress schedule {} task {} baseline diff ok", s.getId(), task.getId());
        }
    }
}
