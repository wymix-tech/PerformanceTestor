package com.perfserver.collector;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.TraceChain;
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
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 调用链上报批量写：分片边界、空批不执行、单条解析失败跳过、字段映射正确。
 *
 * <p>原实现逐条 {@code insert}（单批 500 条 = 500 次往返 + 自增锁竞争），
 * 改为 batchInsert + 分片后须保证：语义不变、条数一致、脏数据不影响整批。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class AgentReportTraceBatchTest {

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

    private Map<String, Object> trace(String traceId) {
        Map<String, Object> item = new HashMap<>();
        item.put("traceId", traceId);
        item.put("rootMethod", "com.demo.OrderService#list");
        item.put("totalMs", 12.5);
        item.put("startTime", 1_700_000_000_000L);
        item.put("tree", Map.of("method", "root", "costMs", 12.5));
        return item;
    }

    private void report(List<Map<String, Object>> items) {
        service.handleReport(Map.of("type", "trace", "appName", "demo-app",
                "targetProjectName", "demo", "items", items), "10.0.0.1");
    }

    @SuppressWarnings("unchecked")
    private List<List<TraceChain>> capturedBatches() {
        ArgumentCaptor<List<TraceChain>> captor = ArgumentCaptor.forClass(List.class);
        verify(traceChainMapper, org.mockito.Mockito.atLeastOnce()).batchInsert(captor.capture());
        return captor.getAllValues();
    }

    /** 500 条应拆成 10 片（每片 50）——不再产生 500 次 DB 往返 */
    @Test
    void splitsIntoChunksOfFifty() {
        List<Map<String, Object>> items = new ArrayList<>();
        for (int i = 0; i < 500; i++) {
            items.add(trace("t-" + i));
        }

        report(items);

        List<List<TraceChain>> batches = capturedBatches();
        assertEquals(10, batches.size());
        for (List<TraceChain> batch : batches) {
            assertEquals(50, batch.size());
        }
        verify(traceChainMapper, never()).insert(any());
    }

    /** 分片边界：50 条整片一片，51 条两片（50 + 1） */
    @Test
    void chunkBoundaryExactMultiple() {
        List<Map<String, Object>> items = new ArrayList<>();
        for (int i = 0; i < 50; i++) {
            items.add(trace("t-" + i));
        }

        report(items);

        assertEquals(1, capturedBatches().size());
    }

    @Test
    void chunkBoundaryOneOver() {
        List<Map<String, Object>> items = new ArrayList<>();
        for (int i = 0; i < 51; i++) {
            items.add(trace("t-" + i));
        }

        report(items);

        List<List<TraceChain>> batches = capturedBatches();
        assertEquals(2, batches.size());
        assertEquals(50, batches.get(0).size());
        assertEquals(1, batches.get(1).size());
    }

    /** 空列表不执行任何写入 */
    @Test
    void emptyListSkipsInsert() {
        report(List.of());

        verify(traceChainMapper, never()).batchInsert(any());
        verify(traceChainMapper, never()).insert(any());
    }

    /** 非 Map 项 / 解析异常项被跳过，不阻断整批 */
    @Test
    void skipsInvalidItemsWithoutAbortingBatch() {
        List<Object> items = new ArrayList<>();
        items.add("not-a-map");
        items.add(trace("ok-1"));
        // tree 含不可序列化对象（Jackson 直接抛 JsonMappingException）→ 该条应被跳过
        Map<String, Object> bad = trace("bad");
        bad.put("tree", Map.of("method", new Object()));
        items.add(bad);
        items.add(trace("ok-2"));

        service.handleReport(Map.of("type", "trace", "appName", "demo-app",
                "targetProjectName", "demo", "items", items), "10.0.0.1");

        List<List<TraceChain>> batches = capturedBatches();
        List<TraceChain> saved = batches.get(0);
        assertEquals(2, saved.size());
        assertEquals("ok-1", saved.get(0).getTraceId());
        assertEquals("ok-2", saved.get(1).getTraceId());
    }

    /** 字段映射：appName 强制取上报应用名，tree 序列化为 trace_json，时间戳转 LocalDateTime */
    @Test
    void mapsFieldsCorrectly() {
        report(List.of(trace("t-1")));

        TraceChain saved = capturedBatches().get(0).get(0);
        assertEquals("demo-app", saved.getAppName());
        assertEquals("t-1", saved.getTraceId());
        assertEquals("com.demo.OrderService#list", saved.getRootMethod());
        assertEquals(12.5, saved.getTotalMs());
        assertTrue(saved.getTraceJson().contains("\"method\":\"root\""));
        org.junit.jupiter.api.Assertions.assertNotNull(saved.getStartTime());
    }

    /** tree 缺失时写入占位 JSON，不落 null（列 NOT NULL） */
    @Test
    void missingTreeWritesPlaceholder() {
        Map<String, Object> item = trace("t-1");
        item.remove("tree");

        report(List.of(item));

        assertEquals("{}", capturedBatches().get(0).get(0).getTraceJson());
    }

    /** JVM 指标同样走分片：201 条 = 2 片，不再一次性拼超长语句 */
    @Test
    void jvmMetricsAlsoChunked() {
        List<Map<String, Object>> items = new ArrayList<>();
        for (int i = 0; i < 201; i++) {
            Map<String, Object> m = new HashMap<>();
            m.put("collectTime", 1_700_000_000_000L);
            m.put("heapUsedMb", 100.0);
            items.add(m);
        }

        service.handleReport(Map.of("type", "jvm", "appName", "demo-app",
                "targetProjectName", "demo", "items", items), "10.0.0.1");

        ArgumentCaptor<List<com.perfserver.entity.JvmMetric>> captor = ArgumentCaptor.forClass(List.class);
        verify(jvmMetricMapper, times(2)).batchInsert(captor.capture());
        assertEquals(200, captor.getAllValues().get(0).size());
        assertEquals(1, captor.getAllValues().get(1).size());
    }

    /** 未知项目：整批丢弃，一条不落库（守门语义不变） */
    @Test
    void unknownProjectDropsEverything() {
        when(projectRegistry.exists("unknown")).thenReturn(false);

        service.handleReport(Map.of("type", "trace", "appName", "demo-app",
                "targetProjectName", "unknown", "items", List.of(trace("t-1"))), "10.0.0.1");

        verify(traceChainMapper, never()).batchInsert(any());
        verify(traceChainMapper, never()).insert(any());
        assertNull(null); // 保持断言风格一致
    }
}
