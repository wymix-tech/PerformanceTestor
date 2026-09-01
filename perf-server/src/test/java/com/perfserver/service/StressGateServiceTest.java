package com.perfserver.service;

import com.perfserver.entity.StressTask;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.notify.NotifyService;
import com.perfserver.stress.StressTaskRequest;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.time.LocalDateTime;
import java.util.ArrayList;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.contains;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * StressGateService 单元测试：用桩替身模拟任务执行与基线比对，覆盖各判定分支。
 */
class StressGateServiceTest {

    /** 组装被测服务：任务服务桩会在 createAndRun 时把任务标为 finished */
    private static StressGateService gateWith(StressTaskService taskService,
                                              StressTaskMapper taskMapper,
                                              BaselineCompareService baselineService,
                                              NotifyService notifyService) {
        StressGateService gate = new StressGateService(taskService, taskMapper, baselineService,
                notifyService);
        // 收尾余量调至最小，避免超时用例真实等待数十秒
        gate.setTimeoutOverheadSec(1);
        return gate;
    }

    @Test
    void passesWhenNoBaselineAndPolicyIsLenient() {
        Fixture f = fixture(diff(true, false, 0L));
        StressGateService.GateVerdict v = f.gate.runGate(req(), 0, 0, false, null);

        assertTrue(v.passed, "无基线且策略宽松时应放行");
        assertTrue(v.noBaseline);
        assertEquals("finished", v.status);
        assertTrue(v.summary.contains("门禁放行"));
        verify(f.notifier, never()).send(any(), any(), any());
    }

    @Test
    void failsWhenNoBaselineAndPolicyIsStrict() {
        Fixture f = fixture(diff(true, false, 0L));
        StressGateService.GateVerdict v = f.gate.runGate(req(), 0, 0, true, null);

        assertFalse(v.passed, "无基线且策略严格时应失败");
        assertTrue(v.summary.contains("fail-on-no-baseline"));
        // webhook 为 null 时仍会调用通知服务（由服务内部静默跳过），此处只校验调用发生
        verify(f.notifier).send(isNull(), contains("性能门禁未通过"), anyString());
    }

    @Test
    void failsOnRegressionAndNotifies() {
        BaselineCompareService.BaselineDiff diff = diff(false, true, 77L);
        diff.rows.add(new BaselineCompareService.DiffRow("QPS", "100", "80", "-20.0%", false, true));
        Fixture f = fixture(diff);

        StressGateService.GateVerdict v = f.gate.runGate(req(), 0, 0, false, "https://hook");

        assertFalse(v.passed);
        assertTrue(v.regressed);
        assertEquals(77L, v.baselineTaskId);
        assertTrue(v.summary.contains("性能回归"));
        assertTrue(v.summary.contains("#77"));
        // 回归明细应进入告警正文
        ArgumentCaptor<String> body = ArgumentCaptor.forClass(String.class);
        verify(f.notifier).send(eq("https://hook"), contains("性能门禁未通过"), body.capture());
        assertTrue(body.getValue().contains("QPS"), "告警正文应含回归明细");
    }

    @Test
    void passesWhenNoRegression() {
        Fixture f = fixture(diff(false, false, 88L));
        StressGateService.GateVerdict v = f.gate.runGate(req(), 0, 0, false, "https://hook");

        assertTrue(v.passed);
        assertFalse(v.regressed);
        assertEquals(88L, v.baselineTaskId);
        assertTrue(v.summary.contains("性能达标"));
        verify(f.notifier, never()).send(any(), any(), any());
    }

    @Test
    void failsWhenTaskDoesNotFinish() {
        Fixture f = fixture(diff(false, false, 0L));
        // 任务一直 running：应超时判失败
        when(f.taskMapper.findById(anyLong())).thenReturn(task("running", null));

        StressGateService.GateVerdict v = f.gate.runGate(req(), 0, 1, false, null);

        assertFalse(v.passed);
        assertEquals("timeout", v.status);
        assertTrue(v.summary.contains("未"), "结论文案应说明未在超时内完成，实际：" + v.summary);
    }

    @Test
    void failsWhenTaskFailed() {
        Fixture f = fixture(diff(false, false, 0L));
        when(f.taskMapper.findById(anyLong())).thenReturn(task("failed", null));

        StressGateService.GateVerdict v = f.gate.runGate(req(), 0, 0, false, null);

        assertFalse(v.passed);
        assertEquals("failed", v.status);
    }

    @Test
    void forwardsBaselineTaskIdToCompareService() {
        Fixture f = fixture(diff(false, false, 0L));
        f.gate.runGate(req(), 123L, 0, false, null);

        ArgumentCaptor<Long> captor = ArgumentCaptor.forClass(Long.class);
        verify(f.baseline).diff(anyLong(), captor.capture());
        assertEquals(123L, captor.getValue());
    }

    @Test
    void notifyFailureDoesNotAffectVerdict() {
        Fixture f = fixture(diff(false, true, 5L));
        doAnswer(inv -> {
            throw new RuntimeException("webhook unreachable");
        }).when(f.notifier).send(any(), any(), any());

        StressGateService.GateVerdict v = f.gate.runGate(req(), 0, 0, false, "https://hook");

        // 告警发送失败不得改变门禁结论
        assertFalse(v.passed);
        assertTrue(v.regressed);
    }

    @Test
    void verdictExposesTaskIdentityAndRows() {
        BaselineCompareService.BaselineDiff diff = diff(false, false, 9L);
        diff.rows.add(new BaselineCompareService.DiffRow("QPS", "100", "110", "+10.0%", true, false));
        Fixture f = fixture(diff);

        StressGateService.GateVerdict v = f.gate.runGate(req(), 0, 0, false, null);

        assertEquals("gate-task", v.taskName);
        assertEquals(1, v.rows.size(), "比对明细应透传给调用方");
        assertEquals("QPS", v.rows.get(0).label);
    }

    // ---------- 测试替身 ----------

    /** 测试替身集合（组件名不能取 notify：Object 的 final 方法） */
    private record Fixture(StressGateService gate, StressTaskMapper taskMapper,
                           BaselineCompareService baseline, NotifyService notifier) {
    }

    private static Fixture fixture(BaselineCompareService.BaselineDiff diff) {
        StressTaskService taskService = mock(StressTaskService.class);
        StressTaskMapper taskMapper = mock(StressTaskMapper.class);
        BaselineCompareService baselineService = mock(BaselineCompareService.class);
        NotifyService notifyService = mock(NotifyService.class);

        StressTask created = task("running", null);
        when(taskService.createAndRun(any())).thenReturn(created);
        // 默认：任务最终成功完成
        when(taskMapper.findById(anyLong())).thenReturn(task("finished", "{}"));
        when(baselineService.diff(anyLong(), anyLong())).thenReturn(diff);

        return new Fixture(gateWith(taskService, taskMapper, baselineService, notifyService),
                taskMapper, baselineService, notifyService);
    }

    /** 最小耗时请求：预热/爬坡归零，避免超时下限用例真实等待 */
    private static StressTaskRequest req() {
        StressTaskRequest request = StressTaskRequest.single("http://localhost/api/orders", 2, 1);
        request.setWarmupSec(0);
        request.setRampUpSec(0);
        return request;
    }

    private static BaselineCompareService.BaselineDiff diff(boolean noBaseline, boolean regressed,
                                                            long baselineId) {
        BaselineCompareService.BaselineDiff d = new BaselineCompareService.BaselineDiff();
        d.noBaseline = noBaseline;
        d.regressed = regressed;
        d.rows = new ArrayList<>();
        if (baselineId > 0) {
            d.baseline = task("finished", "{}");
            d.baseline.setId(baselineId);
        }
        return d;
    }

    private static StressTask task(String status, String resultJson) {
        StressTask t = new StressTask();
        t.setId(0L);
        t.setStatus(status);
        t.setTaskName("gate-task");
        t.setAppName("demo-app");
        t.setTargetUrl("http://localhost/api/orders");
        t.setResultJson(resultJson);
        t.setCreateTime(LocalDateTime.now());
        t.setFinishTime(LocalDateTime.now());
        return t;
    }
}
