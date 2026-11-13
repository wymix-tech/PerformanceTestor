package com.perfserver.service;

import com.perfserver.common.page.CursorCodec;
import com.perfserver.common.page.CursorPage;
import com.perfserver.entity.JvmMetric;
import com.perfserver.entity.SlowCommand;
import com.perfserver.entity.SlowSql;
import com.perfserver.entity.TraceChain;
import com.perfserver.mapper.AppInstanceMapper;
import com.perfserver.mapper.JvmMetricMapper;
import com.perfserver.mapper.SlowCommandMapper;
import com.perfserver.mapper.SlowSqlMapper;
import com.perfserver.mapper.TraceChainMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentMatchers;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 游标分页核心语义：多取一条判 hasMore、末页无游标、游标透传、size 上限与旧 limit 兼容。
 *
 * <p>分页最易错的不是"取到数据"，而是边界：把"正好取满"判成"还有下一页"会让前端
 * 多发一次必空请求；把"还有下一页"判成末页则直接丢数据。故这里逐项锁死。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class MetricQueryServicePagingTest {

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

    private MetricQueryService service;

    @BeforeEach
    void setUp() {
        service = new MetricQueryService(appInstanceMapper, jvmMetricMapper, traceChainMapper,
                slowSqlMapper, slowCommandMapper);
    }

    private static List<TraceChain> traces(int count, LocalDateTime base) {
        List<TraceChain> list = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            TraceChain t = new TraceChain();
            t.setId((long) (count - i));
            t.setStartTime(base.minusSeconds(i));
            list.add(t);
        }
        return list;
    }

    // ==================== hasMore 判定 ====================

    /**
     * 多取一条判 hasMore：请求 size=2 时底层取 3 条，返回 2 条 + hasMore=true + 非空游标。
     */
    @Test
    void reportsHasMoreWhenExtraRowFetched() {
        LocalDateTime base = LocalDateTime.of(2026, 9, 24, 12, 0, 0);
        when(traceChainMapper.queryByCursor(anyString(), any(), any(), any(), any(), any(), anyInt()))
                .thenReturn(traces(3, base));

        CursorPage<TraceChain> page = service.tracesPage("app", null, base.minusHours(1), base, 2, null, null);

        assertEquals(2, page.getItems().size(), "多取的一条必须被丢弃");
        assertTrue(page.isHasMore());
        assertNotNull(page.getNextCursor());
        // 底层确实按 size+1 取数，否则无法区分"正好取满"与"还有下一页"
        verify(traceChainMapper).queryByCursor(eq("app"), isNull(), any(), any(), isNull(), isNull(), eq(3));
    }

    /** 正好取满：底层只回 size 条 → hasMore=false 且游标为 null（前端据此停止加载） */
    @Test
    void reportsNoMoreWhenExactlyFilled() {
        LocalDateTime base = LocalDateTime.of(2026, 9, 24, 12, 0, 0);
        when(traceChainMapper.queryByCursor(anyString(), any(), any(), any(), any(), any(), anyInt()))
                .thenReturn(traces(2, base));

        CursorPage<TraceChain> page = service.tracesPage("app", null, base.minusHours(1), base, 2, null, null);

        assertEquals(2, page.getItems().size());
        assertFalse(page.isHasMore());
        assertNull(page.getNextCursor(), "无下一页时不应给出游标，避免前端拿它再请求一次");
    }

    @Test
    void emptyResultYieldsEmptyPage() {
        LocalDateTime base = LocalDateTime.of(2026, 9, 24, 12, 0, 0);
        when(traceChainMapper.queryByCursor(anyString(), any(), any(), any(), any(), any(), anyInt()))
                .thenReturn(List.of());

        CursorPage<TraceChain> page = service.tracesPage("app", null, base.minusHours(1), base, 50, null, null);

        assertTrue(page.getItems().isEmpty());
        assertFalse(page.isHasMore());
        assertNull(page.getNextCursor());
    }

    /** mapper 返回 null（极端情况）不能让 NPE 冒到 Controller */
    @Test
    void nullRowsTreatedAsEmpty() {
        LocalDateTime base = LocalDateTime.of(2026, 9, 24, 12, 0, 0);
        when(traceChainMapper.queryByCursor(anyString(), any(), any(), any(), any(), any(), anyInt()))
                .thenReturn(null);

        CursorPage<TraceChain> page = service.tracesPage("app", null, base.minusHours(1), base, 50, null, null);
        assertTrue(page.getItems().isEmpty());
    }

    // ==================== 游标透传 ====================

    /**
     * 游标必须解码成 {@code (时间, id)} 传给 Mapper：只传时间会漏掉并列时间点的记录。
     */
    @Test
    void decodesCursorIntoTimeAndId() {
        LocalDateTime base = LocalDateTime.of(2026, 9, 24, 12, 0, 0);
        CursorCodec.Cursor cursor = new CursorCodec.Cursor(base.minusMinutes(5), 77L);
        String encoded = CursorCodec.encode(cursor.time(), cursor.id());

        when(traceChainMapper.queryByCursor(anyString(), any(), any(), any(), any(), any(), anyInt()))
                .thenReturn(List.of());

        service.tracesPage("app", null, base.minusHours(1), base, 10, null, encoded);

        verify(traceChainMapper).queryByCursor(eq("app"), isNull(), any(), any(),
                eq(cursor.time()), eq(77L), eq(11));
    }

    /** 非法游标按"无游标"处理（首屏），而不是报错——前端手改 URL 不该变成页面报错 */
    @Test
    void invalidCursorFallsBackToFirstPage() {
        LocalDateTime base = LocalDateTime.of(2026, 9, 24, 12, 0, 0);
        when(traceChainMapper.queryByCursor(anyString(), any(), any(), any(), any(), any(), anyInt()))
                .thenReturn(List.of());

        service.tracesPage("app", null, base.minusHours(1), base, 10, null, "!!!broken!!!");

        verify(traceChainMapper).queryByCursor(eq("app"), isNull(), any(), any(),
                isNull(), isNull(), eq(11));
    }

    // ==================== 各列表 size / 兼容 ====================

    @Test
    void clampsSizeToMax() {
        when(traceChainMapper.queryByCursor(anyString(), any(), any(), any(), any(), any(), anyInt()))
                .thenReturn(List.of());
        service.tracesPage("app", null, null, null, 99999, null, null);
        verify(traceChainMapper).queryByCursor(anyString(), any(), any(), any(), any(), any(),
                eq(com.perfserver.common.page.CursorPageRequest.MAX_SIZE + 1));
    }

    /** 旧前端只传 limit：仍按 limit 取数，不因改造把存量调用打成默认 50 */
    @Test
    void legacyLimitStillHonoured() {
        when(slowSqlMapper.queryByCursor(anyString(), any(), any(), any(), any(), any(), anyInt()))
                .thenReturn(List.of());
        service.slowSqlsPage("app", null, null, null, 0, 7, null);
        verify(slowSqlMapper).queryByCursor(eq("app"), any(), any(), isNull(), isNull(), isNull(), eq(8));
    }

    @Test
    void jvmMetricsPageUsesCollectTimeAndId() {
        LocalDateTime base = LocalDateTime.of(2026, 9, 24, 12, 0, 0);
        JvmMetric m = new JvmMetric();
        m.setId(11L);
        m.setCollectTime(base);
        when(jvmMetricMapper.queryByCursor(anyString(), any(), any(), any(), any(), anyInt()))
                .thenReturn(List.of(m, m, m));

        CursorPage<JvmMetric> page = service.jvmMetricsPage("app", base.minusHours(1), base, 2, null);

        assertEquals(2, page.getItems().size());
        assertTrue(page.isHasMore());
        // 游标取"实际返回的最后一条"，不是多取的那条，否则会跳过一条记录
        assertEquals(base, CursorCodec.decode(page.getNextCursor()).time());
    }

    @Test
    void slowCommandsPageForwardsTypeFilter() {
        when(slowCommandMapper.queryByCursor(anyString(), any(), any(), any(), any(), any(), any(), anyInt()))
                .thenReturn(List.of());
        service.slowCommandsPage("app", "redis", null, null, 10.0, 20, null, null);
        verify(slowCommandMapper).queryByCursor(eq("app"), eq("redis"), any(), any(), eq(10.0),
                isNull(), isNull(), eq(21));
    }

    @Test
    void slowSqlPageMapsEntityKeys() {
        LocalDateTime base = LocalDateTime.of(2026, 9, 24, 12, 0, 0);
        SlowSql s1 = new SlowSql();
        s1.setId(5L);
        s1.setExecuteTime(base);
        SlowSql s2 = new SlowSql();
        s2.setId(4L);
        s2.setExecuteTime(base.minusSeconds(1));
        SlowSql extra = new SlowSql();
        extra.setId(3L);
        extra.setExecuteTime(base.minusSeconds(2));
        when(slowSqlMapper.queryByCursor(anyString(), any(), any(), any(), any(), any(), anyInt()))
                .thenReturn(List.of(s1, s2, extra));

        CursorPage<SlowSql> page = service.slowSqlsPage("app", base.minusHours(1), base, null, 2, null, null);

        assertTrue(page.isHasMore());
        // 游标必须指向返回列表的最后一条（s2），指到 extra 会永久跳过 s2
        CursorCodec.Cursor cursor = CursorCodec.decode(page.getNextCursor());
        assertEquals(4L, cursor.id());
        assertEquals(base.minusSeconds(1), cursor.time());
    }

    @Test
    void slowCommandPageMapsEntityKeys() {
        LocalDateTime base = LocalDateTime.of(2026, 9, 24, 12, 0, 0);
        SlowCommand c = new SlowCommand();
        c.setId(9L);
        c.setExecuteTime(base);
        SlowCommand extra = new SlowCommand();
        extra.setId(8L);
        extra.setExecuteTime(base.minusSeconds(5));
        when(slowCommandMapper.queryByCursor(anyString(), any(), any(), any(), any(), any(), any(), anyInt()))
                .thenReturn(List.of(c, extra));

        CursorPage<SlowCommand> page = service.slowCommandsPage("app", null, base.minusHours(1), base,
                null, 1, null, null);

        assertTrue(page.isHasMore());
        assertEquals(9L, CursorCodec.decode(page.getNextCursor()).id());
    }

    /** null 主键（历史数据 id 缺失）不能 NPE，兜底为 0 */
    @Test
    void nullIdFallbacksToZero() {
        LocalDateTime base = LocalDateTime.of(2026, 9, 24, 12, 0, 0);
        JvmMetric m1 = new JvmMetric();
        m1.setCollectTime(base);
        JvmMetric m2 = new JvmMetric();
        m2.setCollectTime(base.minusSeconds(1));
        when(jvmMetricMapper.queryByCursor(anyString(), any(), any(), any(), any(), anyInt()))
                .thenReturn(List.of(m1, m2, new JvmMetric()));

        CursorPage<JvmMetric> page = service.jvmMetricsPage("app", base.minusHours(1), base, 2, null);

        assertEquals(0L, CursorCodec.decode(page.getNextCursor()).id());
    }

    /** 默认时间窗口：未传 start/end 时回退最近 1 小时，避免全表扫 */
    @Test
    void defaultsTimeWindow() {
        when(traceChainMapper.queryByCursor(anyString(), any(), ArgumentMatchers.any(), ArgumentMatchers.any(),
                any(), any(), anyInt())).thenReturn(List.of());
        service.tracesPage("app", null, null, null, 10, null, null);
        verify(traceChainMapper).queryByCursor(eq("app"), isNull(),
                ArgumentMatchers.any(LocalDateTime.class), ArgumentMatchers.any(LocalDateTime.class),
                isNull(), isNull(), eq(11));
    }
}
