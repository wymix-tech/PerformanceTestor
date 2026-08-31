package com.perfserver.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.entity.StressTask;
import com.perfserver.mapper.PerformanceIssueMapper;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.stress.StressTaskRequest;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.time.LocalDateTime;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 修复效果自动验证：达标 / 未达标 / 无快照 三分支 + 重放场景参数一致性。
 */
class IssueVerifyServiceTest {

    private PerformanceIssueMapper issueMapper;
    private StressTaskMapper taskMapper;
    private StressTaskService stressTaskService;
    private IssueVerifyService service;

    @BeforeEach
    void setUp() {
        issueMapper = mock(PerformanceIssueMapper.class);
        taskMapper = mock(StressTaskMapper.class);
        stressTaskService = mock(StressTaskService.class);
        BaselineCompareService baseline = new BaselineCompareService(taskMapper, new ObjectMapper());
        service = new IssueVerifyService(issueMapper, taskMapper, stressTaskService, baseline,
                new ObjectMapper(), 300);
    }

    /** 达标：P95 降幅 ≥ 30% 且无回归 → 自动 resolved */
    @Test
    void passWhenP95DropsOverThreshold() {
        PerformanceIssue issue = issue(10, "analyzed", 1L);
        when(issueMapper.findById(10L)).thenReturn(issue);
        when(taskMapper.findById(1L)).thenReturn(finishedTask(1, "{\"qps\":100,\"p95Ms\":200,\"p99Ms\":400,\"errorRate\":0}"));
        // 重放任务 2：P95 100ms（降 50%），QPS 提升至 120
        when(taskMapper.findById(2L)).thenReturn(finishedTask(2, "{\"qps\":120,\"p95Ms\":100,\"p99Ms\":200,\"errorRate\":0}"));
        when(stressTaskService.createAndRun(org.mockito.ArgumentMatchers.any(StressTaskRequest.class)))
                .thenReturn(pendingTask(2));

        IssueVerifyService.VerifyResult r = service.verify(10);

        assertTrue(r.isPassed(), "P95 降 50% 且无回归应达标");
        assertEquals("resolved", r.getStatus());
        assertFalse(r.isRegressed());
        assertEquals(200.0, r.getP95Before());
        assertEquals(100.0, r.getP95After());
        assertEquals(50.0, r.getP95DropPct());
        assertEquals(1L, r.getSnapshotTaskId());
        assertEquals(2L, r.getReplayTaskId());
        assertEquals(4, r.getRows().size(), "需回填 QPS/P95/P99/错误率 四行对比明细");
        verify(issueMapper).updateVerify(eq(10L), org.mockito.ArgumentMatchers.anyString(),
                org.mockito.ArgumentMatchers.any(), eq("resolved"));
    }

    /** 未达标：P95 降幅不足（10%）→ 保持原状态 */
    @Test
    void notPassWhenP95DropBelowThreshold() {
        PerformanceIssue issue = issue(11, "fixed", 1L);
        when(issueMapper.findById(11L)).thenReturn(issue);
        when(taskMapper.findById(1L)).thenReturn(finishedTask(1, "{\"qps\":100,\"p95Ms\":100,\"p99Ms\":200,\"errorRate\":0}"));
        when(taskMapper.findById(2L)).thenReturn(finishedTask(2, "{\"qps\":100,\"p95Ms\":90,\"p99Ms\":180,\"errorRate\":0}"));
        when(stressTaskService.createAndRun(org.mockito.ArgumentMatchers.any(StressTaskRequest.class)))
                .thenReturn(pendingTask(2));

        IssueVerifyService.VerifyResult r = service.verify(11);

        assertFalse(r.isPassed(), "P95 仅降 10%（< 30%）不应达标");
        assertEquals("fixed", r.getStatus(), "未达标时保持原状态");
        assertEquals(10.0, r.getP95DropPct());
        assertTrue(r.getMessage().contains("验证未达标"));
        verify(issueMapper).updateVerify(eq(11L), org.mockito.ArgumentMatchers.anyString(),
                org.mockito.ArgumentMatchers.any(), eq("fixed"));
    }

    /** 未达标：P95 达标但存在回归（QPS 大跌）→ 不关闭 */
    @Test
    void notPassWhenRegressionDetected() {
        PerformanceIssue issue = issue(12, "fixed", 1L);
        when(issueMapper.findById(12L)).thenReturn(issue);
        when(taskMapper.findById(1L)).thenReturn(finishedTask(1, "{\"qps\":100,\"p95Ms\":200,\"p99Ms\":400,\"errorRate\":0}"));
        // P95 降 50% 达标，但 QPS 由 100 跌到 60（-40% > 10% 阈值）判定回归
        when(taskMapper.findById(2L)).thenReturn(finishedTask(2, "{\"qps\":60,\"p95Ms\":100,\"p99Ms\":200,\"errorRate\":0}"));
        when(stressTaskService.createAndRun(org.mockito.ArgumentMatchers.any(StressTaskRequest.class)))
                .thenReturn(pendingTask(2));

        IssueVerifyService.VerifyResult r = service.verify(12);

        assertFalse(r.isPassed(), "存在指标回归不应达标");
        assertTrue(r.isRegressed());
        assertEquals("fixed", r.getStatus());
        assertTrue(r.getMessage().contains("存在指标回归"));
    }

    /** 无快照：问题未关联压测任务 → 标记 noSnapshot，不发起压测也不写库 */
    @Test
    void noSnapshotWhenIssueHasNoStressTask() {
        when(issueMapper.findById(20L)).thenReturn(issue(20, "analyzed", 0L));

        IssueVerifyService.VerifyResult r = service.verify(20);

        assertTrue(r.isNoSnapshot());
        assertFalse(r.isPassed());
        assertEquals("analyzed", r.getStatus(), "无快照时保持问题原状态");
        assertTrue(r.getMessage().contains("未关联压测场景快照"));
        verify(stressTaskService, never()).createAndRun(org.mockito.ArgumentMatchers.any());
        verify(issueMapper, never()).updateVerify(anyLong(), org.mockito.ArgumentMatchers.anyString(),
                org.mockito.ArgumentMatchers.any(), org.mockito.ArgumentMatchers.any());
    }

    /** 无快照：快照任务已删除 → 同上 */
    @Test
    void noSnapshotWhenSnapshotTaskMissing() {
        when(issueMapper.findById(21L)).thenReturn(issue(21, "analyzed", 99L));
        when(taskMapper.findById(99L)).thenReturn(null);

        IssueVerifyService.VerifyResult r = service.verify(21);

        assertTrue(r.isNoSnapshot());
        assertTrue(r.getMessage().contains("已不存在"));
        verify(stressTaskService, never()).createAndRun(org.mockito.ArgumentMatchers.any());
    }

    /** 重放压测失败（任务 finished 失败态）→ 标记 replayFailed，状态不变 */
    @Test
    void replayFailedWhenStressTaskFails() {
        when(issueMapper.findById(30L)).thenReturn(issue(30, "fixed", 1L));
        when(taskMapper.findById(1L)).thenReturn(finishedTask(1, "{\"qps\":100,\"p95Ms\":200}"));
        StressTask failed = finishedTask(2, "{}");
        failed.setStatus("failed");
        failed.setErrorMsg("目标服务不可达");
        // createAndRun 返回后轮询直接命中 failed
        when(taskMapper.findById(2L)).thenReturn(failed);
        when(stressTaskService.createAndRun(org.mockito.ArgumentMatchers.any(StressTaskRequest.class)))
                .thenReturn(pendingTask(2));

        IssueVerifyService.VerifyResult r = service.verify(30);

        assertTrue(r.isReplayFailed());
        assertFalse(r.isPassed());
        assertEquals("fixed", r.getStatus());
        assertTrue(r.getMessage().contains("目标服务不可达"));
    }

    /** 重放场景与快照保持一致（同 URL/并发/时长/方法/限流） */
    @Test
    void replaysSameSceneAsSnapshot() {
        PerformanceIssue issue = issue(40, "fixed", 1L);
        when(issueMapper.findById(40L)).thenReturn(issue);
        StressTask snapshot = finishedTask(1, "{\"qps\":100,\"p95Ms\":200,\"p99Ms\":400,\"errorRate\":0}");
        snapshot.setTargetUrl("http://127.0.0.1:8081/api/user/list?limit=200");
        snapshot.setMethod("GET");
        snapshot.setConcurrency(20);
        snapshot.setDurationSec(15);
        snapshot.setQpsLimit(0);
        snapshot.setAppName("perf-demo");
        when(taskMapper.findById(1L)).thenReturn(snapshot);
        when(taskMapper.findById(2L)).thenReturn(finishedTask(2, "{\"qps\":150,\"p95Ms\":90,\"p99Ms\":180,\"errorRate\":0}"));
        when(stressTaskService.createAndRun(org.mockito.ArgumentMatchers.any(StressTaskRequest.class)))
                .thenReturn(pendingTask(2));

        service.verify(40);

        ArgumentCaptor<StressTaskRequest> captor = ArgumentCaptor.forClass(StressTaskRequest.class);
        verify(stressTaskService).createAndRun(captor.capture());
        StressTaskRequest req = captor.getValue();
        assertEquals("http://127.0.0.1:8081/api/user/list?limit=200", req.getTargetUrl());
        assertEquals("GET", req.getMethod());
        assertEquals(20, req.getConcurrency());
        assertEquals(15, req.getDurationSec());
        assertEquals(0, req.getQpsLimit());
        assertEquals("perf-demo", req.getAppName());
    }

    /** 达标结论落库为 JSON 且含对比明细，便于前端直接渲染 */
    @Test
    void persistsVerifyJsonWithRows() {
        when(issueMapper.findById(50L)).thenReturn(issue(50, "fixed", 1L));
        when(taskMapper.findById(1L)).thenReturn(finishedTask(1, "{\"qps\":100,\"p95Ms\":200,\"p99Ms\":400,\"errorRate\":0}"));
        when(taskMapper.findById(2L)).thenReturn(finishedTask(2, "{\"qps\":120,\"p95Ms\":100,\"p99Ms\":200,\"errorRate\":0}"));
        when(stressTaskService.createAndRun(org.mockito.ArgumentMatchers.any(StressTaskRequest.class)))
                .thenReturn(pendingTask(2));

        service.verify(50);

        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        verify(issueMapper).updateVerify(eq(50L), jsonCaptor.capture(),
                org.mockito.ArgumentMatchers.any(), eq("resolved"));
        String json = jsonCaptor.getValue();
        assertTrue(json.contains("\"passed\":true"));
        assertTrue(json.contains("\"p95DropPct\":50.0"));
        assertTrue(json.contains("\"rows\""));
        assertTrue(json.contains("P95 (ms)"));
    }

    /** 问题不存在：直接抛错（由全局异常处理为 400） */
    @Test
    void throwsWhenIssueMissing() {
        when(issueMapper.findById(60L)).thenReturn(null);

        org.junit.jupiter.api.Assertions.assertThrows(IllegalArgumentException.class, () -> service.verify(60));
    }

    // ---------------- helpers ----------------

    private static PerformanceIssue issue(long id, String status, long stressTaskId) {
        PerformanceIssue issue = new PerformanceIssue();
        issue.setId(id);
        issue.setStatus(status);
        issue.setStressTaskId(stressTaskId);
        issue.setIssueType("stress_bottleneck");
        return issue;
    }

    private static StressTask finishedTask(long id, String resultJson) {
        StressTask t = new StressTask();
        t.setId(id);
        t.setStatus("finished");
        t.setResultJson(resultJson);
        t.setTargetUrl("http://h/api");
        t.setMethod("GET");
        t.setConcurrency(10);
        t.setDurationSec(10);
        t.setQpsLimit(0);
        t.setCreateTime(LocalDateTime.now().minusMinutes(5));
        t.setFinishTime(LocalDateTime.now().minusMinutes(4));
        assertNotNull(t.getFinishTime());
        return t;
    }

    private static StressTask pendingTask(long id) {
        StressTask t = new StressTask();
        t.setId(id);
        t.setStatus("running");
        t.setDurationSec(10);
        assertNull(t.getResultJson());
        return t;
    }
}
