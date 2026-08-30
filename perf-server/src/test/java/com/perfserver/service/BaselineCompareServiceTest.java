package com.perfserver.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.StressTask;
import com.perfserver.mapper.StressTaskMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * 版本基线对比逻辑单元测试：回归阈值判定与自动基线选择。
 */
class BaselineCompareServiceTest {

    private StressTaskMapper taskMapper;
    private BaselineCompareService service;

    @BeforeEach
    void setUp() {
        taskMapper = mock(StressTaskMapper.class);
        service = new BaselineCompareService(taskMapper, new ObjectMapper());
    }

    private static StressTask task(long id, String app, String url, String version,
                                   String resultJson, LocalDateTime finish) {
        StressTask t = new StressTask();
        t.setId(id);
        t.setAppName(app);
        t.setTargetUrl(url);
        t.setAppVersion(version);
        t.setResultJson(resultJson);
        t.setFinishTime(finish);
        t.setCreateTime(finish.plusMinutes(10));
        t.setStatus("finished");
        return t;
    }

    @Test
    void detectsQpsRegression() {
        StressTask baseline = task(1, "app", "http://h/api", "1.0",
                "{\"qps\":100,\"p95Ms\":100,\"p99Ms\":200,\"errorRate\":0}", LocalDateTime.now().minusHours(2));
        StressTask current = task(2, "app", "http://h/api", "1.1",
                "{\"qps\":80,\"p95Ms\":105,\"p99Ms\":210,\"errorRate\":0}", LocalDateTime.now().minusHours(1));
        when(taskMapper.findById(2L)).thenReturn(current);
        when(taskMapper.findById(1L)).thenReturn(baseline);

        BaselineCompareService.BaselineDiff diff = service.diff(2, 1);
        assertFalse(diff.noBaseline);
        assertNotNull(diff.baseline);
        assertTrue(diff.regressed, "QPS 下降 20%（超 10% 阈值）应判定回归");
        assertEquals(4, diff.rows.size());
        // QPS 行显示 -20%
        assertTrue(diff.rows.get(0).deltaPct.contains("-20.0"));
    }

    @Test
    void detectsP95Regression() {
        StressTask baseline = task(1, "app", "http://h/api", "1.0",
                "{\"qps\":100,\"p95Ms\":100,\"p99Ms\":200,\"errorRate\":0}", LocalDateTime.now().minusHours(2));
        StressTask current = task(2, "app", "http://h/api", "1.1",
                "{\"qps\":99,\"p95Ms\":130,\"p99Ms\":260,\"errorRate\":0}", LocalDateTime.now().minusHours(1));
        when(taskMapper.findById(2L)).thenReturn(current);
        when(taskMapper.findById(1L)).thenReturn(baseline);

        assertTrue(service.diff(2, 1).regressed, "P95 上升 30%（超 20% 阈值）应判定回归");
    }

    @Test
    void withinThresholdsIsNotRegressed() {
        StressTask baseline = task(1, "app", "http://h/api", "1.0",
                "{\"qps\":100,\"p95Ms\":100,\"p99Ms\":200,\"errorRate\":1}", LocalDateTime.now().minusHours(2));
        StressTask current = task(2, "app", "http://h/api", "1.1",
                "{\"qps\":105,\"p95Ms\":110,\"p99Ms\":220,\"errorRate\":2}", LocalDateTime.now().minusHours(1));
        when(taskMapper.findById(2L)).thenReturn(current);
        when(taskMapper.findById(1L)).thenReturn(baseline);

        BaselineCompareService.BaselineDiff diff = service.diff(2, 1);
        assertFalse(diff.regressed, "QPS+5% / P95+10% / 错误率+1pct 均在阈值内");
    }

    @Test
    void errorRateRiseOverThresholdIsRegressed() {
        StressTask baseline = task(1, "app", "http://h/api", "1.0",
                "{\"qps\":100,\"p95Ms\":100,\"p99Ms\":200,\"errorRate\":0.5}", LocalDateTime.now().minusHours(2));
        StressTask current = task(2, "app", "http://h/api", "1.1",
                "{\"qps\":100,\"p95Ms\":100,\"p99Ms\":200,\"errorRate\":8}", LocalDateTime.now().minusHours(1));
        when(taskMapper.findById(2L)).thenReturn(current);
        when(taskMapper.findById(1L)).thenReturn(baseline);

        assertTrue(service.diff(2, 1).regressed, "错误率上升 7.5pct（超 5pct 阈值）应判定回归");
    }

    @Test
    void autoBaselinePicksLatestFinishedSameScene() {
        StressTask old = task(1, "app", "http://h/api", "1.0", "{}", LocalDateTime.now().minusDays(3));
        StressTask newer = task(2, "app", "http://h/api", "1.1", "{}", LocalDateTime.now().minusDays(1));
        StressTask otherUrl = task(3, "app", "http://h/other", "1.2", "{}", LocalDateTime.now().minusHours(1));
        StressTask otherApp = task(4, "app2", "http://h/api", "1.2", "{}", LocalDateTime.now().minusHours(1));
        StressTask current = task(5, "app", "http://h/api", "1.3",
                "{\"qps\":100,\"p95Ms\":100,\"p99Ms\":200,\"errorRate\":0}", LocalDateTime.now());
        when(taskMapper.findById(5L)).thenReturn(current);
        when(taskMapper.query(any(), anyInt())).thenReturn(List.of(old, newer, otherUrl, otherApp, current));

        BaselineCompareService.BaselineDiff diff = service.diff(5, 0); // baselineTaskId=0 → 自动
        assertFalse(diff.noBaseline);
        assertEquals(2L, diff.baseline.getId().longValue(), "自动基线应取同应用同 URL 最近完成任务");
    }

    @Test
    void noBaselineWhenFirstRun() {
        StressTask current = task(2, "app", "http://h/api", "1.1", "{}", LocalDateTime.now());
        when(taskMapper.findById(2L)).thenReturn(current);
        when(taskMapper.findById(anyLong())).thenReturn(current);
        when(taskMapper.query(any(), anyInt())).thenReturn(List.of(current));

        assertTrue(service.diff(2, 0).noBaseline, "无可对比历史任务时应标记 noBaseline");
    }
}
