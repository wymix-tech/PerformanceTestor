package com.perfserver.collector;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.SlowCommand;
import com.perfserver.mapper.AppInstanceMapper;
import com.perfserver.mapper.DbConnMapper;
import com.perfserver.mapper.HotspotMethodMapper;
import com.perfserver.mapper.JvmMetricMapper;
import com.perfserver.mapper.SlowCommandMapper;
import com.perfserver.mapper.SlowSqlMapper;
import com.perfserver.mapper.TraceChainMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * Agent 慢命令上报落库单测：字段映射、类型归一、超长截断、缺字段跳过、未知类型不误写。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class AgentReportServiceSlowCommandTest {

    @Mock
    private AppInstanceMapper appInstanceMapper;
    @Mock
    private JvmMetricMapper jvmMetricMapper;
    @Mock
    private TraceChainMapper traceChainMapper;
    @Mock
    private SlowSqlMapper slowSqlMapper;
    @Mock
    private SlowCommandMapper slowCommandMapper;
    @Mock
    private HotspotMethodMapper hotspotMethodMapper;
    @Mock
    private DbConnMapper dbConnMapper;
    @Mock
    private ProjectRegistry projectRegistry;

    private AgentReportService service;

    @BeforeEach
    void setUp() {
        when(projectRegistry.exists(anyString())).thenReturn(true);
        service = new AgentReportService(appInstanceMapper, jvmMetricMapper, traceChainMapper,
                slowSqlMapper, slowCommandMapper, hotspotMethodMapper, dbConnMapper,
                projectRegistry, new ObjectMapper());
    }

    @SuppressWarnings("unchecked")
    private List<SlowCommand> captureSaved() {
        ArgumentCaptor<List<SlowCommand>> captor = ArgumentCaptor.forClass(List.class);
        verify(slowCommandMapper).batchInsert(captor.capture());
        return captor.getValue();
    }

    private void report(String type, List<Map<String, Object>> items) {
        service.handleReport(Map.of("type", type, "appName", "demo-app",
                "targetProjectName", "demo", "items", items), "10.0.0.1");
    }

    @Test
    void savesSlowCommandsWithAllFields() {
        report("slowCommand", List.of(Map.of(
                "type", "redis",
                "command", "get user:123",
                "costMs", 156,
                "traceId", "trace-1",
                "methodChain", List.of("Controller#list", "Service#find"),
                "executeTime", 1_700_000_000_000L)));

        List<SlowCommand> batch = captureSaved();
        assertEquals(1, batch.size());
        SlowCommand c = batch.get(0);
        assertEquals("demo-app", c.getAppName());
        assertEquals("redis", c.getType());
        assertEquals("get user:123", c.getCommand());
        assertEquals(156.0, c.getCostMs());
        assertEquals("trace-1", c.getTraceId());
        assertEquals("[\"Controller#list\",\"Service#find\"]", c.getMethodChain());
        assertTrue(c.getExecuteTime() != null);
    }

    @Test
    void normalizesTypeToLowerCase() {
        report("slowCommand", List.of(Map.of(
                "type", "REDIS", "command", "get k", "costMs", 200, "executeTime", 1_700_000_000_000L)));
        assertEquals("redis", captureSaved().get(0).getType(), "类型应归一为小写");
    }

    @Test
    void truncatesOverlongCommand() {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 5000; i++) {
            sb.append('x');
        }
        report("slowCommand", List.of(Map.of(
                "type", "mq", "command", sb.toString(), "costMs", 300, "executeTime", 1_700_000_000_000L)));

        String command = captureSaved().get(0).getCommand();
        assertEquals(1003, command.length(), "应截断到 1000 + '...'");
        assertTrue(command.endsWith("..."));
    }

    @Test
    void skipsItemsWithoutType() {
        report("slowCommand", List.of(Map.of("command", "get k", "costMs", 100)));
        verifyNoInteractions(slowCommandMapper);
    }

    @Test
    void toleratesNullCommandAndMissingTraceId() {
        report("slowCommand", List.of(Map.of("type", "es", "costMs", 120, "executeTime", 1_700_000_000_000L)));
        SlowCommand c = captureSaved().get(0);
        assertEquals("", c.getCommand(), "缺失命令文本应存空串（列 NOT NULL）");
        assertNull(c.getTraceId(), "未采样时 traceId 为空");
    }

    @Test
    void unknownTypeDoesNotTouchSlowCommandTable() {
        report("somethingElse", List.of(Map.of("type", "redis", "command", "get k")));
        verifyNoInteractions(slowCommandMapper);
    }

    @Test
    void droppedByProjectGateWhenProjectMissing() {
        when(projectRegistry.exists(any())).thenReturn(false);
        report("slowCommand", new ArrayList<>(List.of(Map.of("type", "redis", "command", "get k"))));
        verifyNoInteractions(slowCommandMapper);
    }
}
