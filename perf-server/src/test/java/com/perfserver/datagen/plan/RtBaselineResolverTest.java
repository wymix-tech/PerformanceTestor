package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.StressSample;
import com.perfserver.entity.StressTask;
import com.perfserver.mapper.StressSampleMapper;
import com.perfserver.mapper.StressTaskMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

/** RT 基线三级取值单测：历史压测 → 采样点 → 缺省值，含各级失败降级。 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class RtBaselineResolverTest {

    @Mock
    private StressTaskMapper taskMapper;
    @Mock
    private StressSampleMapper sampleMapper;

    private RtBaselineResolver resolver() {
        return new RtBaselineResolver(taskMapper, sampleMapper, new ObjectMapper());
    }

    private static StressTask task(String resultJson) {
        StressTask t = new StressTask();
        t.setResultJson(resultJson);
        return t;
    }

    private static StressSample sample(Double p50, Double p95) {
        StressSample s = new StressSample();
        s.setP50Ms(p50);
        s.setP95Ms(p95);
        return s;
    }

    @Test
    void samplePointsTakePriorityWhenTaskIdGiven() {
        when(sampleMapper.queryByTask(anyLong(), anyInt()))
                .thenReturn(List.of(sample(120.0, 300.0), sample(180.0, 320.0)));
        var b = resolver().resolve("app", "http://x/api", 7L);
        assertEquals(150.0, b.getRtMs(), 0.001, "取 p50 均值");
        assertEquals(RtBaselineResolver.SRC_SAMPLE, b.getSource());
    }

    @Test
    void historyAvgMsIsUsedWhenNoTaskId() {
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt()))
                .thenReturn(List.of(task("{\"avgMs\":256.0,\"p95Ms\":800.0}")));
        var b = resolver().resolve("app", "http://x/api", null);
        assertEquals(256.0, b.getRtMs(), 0.001);
        assertEquals(RtBaselineResolver.SRC_HISTORY, b.getSource());
    }

    @Test
    void historyFallsBackToP95WhenAvgMsMissing() {
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt()))
                .thenReturn(List.of(task("{\"p95Ms\":800.0}")));
        var b = resolver().resolve("app", "http://x/api", null);
        assertEquals(800.0, b.getRtMs(), 0.001);
    }

    @Test
    void nestedStatsNodeIsParsed() {
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt()))
                .thenReturn(List.of(task("{\"stats\":{\"avgMs\":123.0}}")));
        assertEquals(123.0, resolver().resolve("app", "http://x/api", null).getRtMs(), 0.001);
    }

    @Test
    void defaultsWhenNothingAvailable() {
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt()))
                .thenReturn(List.of());
        var b = resolver().resolve("app", "http://x/api", null);
        assertEquals(RtBaselineResolver.DEFAULT_RT_MS, b.getRtMs(), 0.001);
        assertEquals(RtBaselineResolver.SRC_DEFAULT, b.getSource());
    }

    @Test
    void fallsBackToHistoryWhenSamplesEmpty() {
        when(sampleMapper.queryByTask(anyLong(), anyInt())).thenReturn(List.of());
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt()))
                .thenReturn(List.of(task("{\"avgMs\":90.0}")));
        var b = resolver().resolve("app", "http://x/api", 1L);
        assertEquals(90.0, b.getRtMs(), 0.001);
        assertEquals(RtBaselineResolver.SRC_HISTORY, b.getSource());
    }

    @Test
    void mapperExceptionDegradesToDefaultNotThrow() {
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt()))
                .thenThrow(new RuntimeException("db down"));
        var b = resolver().resolve("app", "http://x/api", null);
        assertEquals(RtBaselineResolver.DEFAULT_RT_MS, b.getRtMs(), 0.001);
        assertEquals(RtBaselineResolver.SRC_DEFAULT, b.getSource());
    }

    @Test
    void valuesAreClamped() {
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt()))
                .thenReturn(List.of(task("{\"avgMs\":999999.0}")));
        assertEquals(RtBaselineResolver.MAX_RT_MS,
                resolver().resolve("app", "http://x/api", null).getRtMs(), 0.001);
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt()))
                .thenReturn(List.of(task("{\"avgMs\":0.01}")));
        assertEquals(RtBaselineResolver.MIN_RT_MS,
                resolver().resolve("app", "http://x/api", null).getRtMs(), 0.001);
    }

    @Test
    void brokenJsonDegradesToDefault() {
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt()))
                .thenReturn(List.of(task("not json")));
        assertEquals(RtBaselineResolver.DEFAULT_RT_MS,
                resolver().resolve("app", "http://x/api", null).getRtMs(), 0.001);
    }

    @Test
    void missingAppNameSkipsHistory() {
        var b = resolver().resolve(null, null, null);
        assertEquals(RtBaselineResolver.SRC_DEFAULT, b.getSource());
    }

    @Test
    void sourceLabelsAreDescribedForUi() {
        assertTrue(RtBaselineResolver.describe(RtBaselineResolver.SRC_HISTORY).contains("历史"));
        assertTrue(RtBaselineResolver.describe(RtBaselineResolver.SRC_SAMPLE).contains("采样"));
        assertTrue(RtBaselineResolver.describe(RtBaselineResolver.SRC_DEFAULT).contains("默认"));
    }
}
