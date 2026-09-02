package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

/**
 * seq 起始值刷新单测：验证「方案只存 seqPrefix、执行时刷新 seqStart」这条约束，
 * 以及探测失败时的回退与告警。
 *
 * <p>唯一键无碰撞的真实写入行为由引擎侧 H2 测试（{@code SeqRuleTest}）覆盖，
 * 本类聚焦解析器的取值与降级逻辑。</p>
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class SeqStartResolverTest {

    @Mock
    private Connection conn;
    @Mock
    private PreparedStatement ps;
    @Mock
    private ResultSet rs;

    private final SeqStartResolver resolver = new SeqStartResolver(new ObjectMapper());

    private void stubMax(long value) throws Exception {
        when(conn.prepareStatement(anyString())).thenReturn(ps);
        when(ps.executeQuery()).thenReturn(rs);
        when(rs.next()).thenReturn(true);
        when(rs.getObject(1)).thenReturn(value);
    }

    private void stubMaxNull() throws Exception {
        when(conn.prepareStatement(anyString())).thenReturn(ps);
        when(ps.executeQuery()).thenReturn(rs);
        when(rs.next()).thenReturn(true);
        when(rs.getObject(1)).thenReturn(null);
    }

    private JsonNode parse(String json) throws Exception {
        return new ObjectMapper().readTree(json);
    }

    @Test
    void numericColumnStartIsMaxPlusOne() throws Exception {
        stubMax(35L);
        var out = resolver.refresh(conn, "perf_order",
                "{\"seq_no\":{\"type\":\"seq\",\"seqPrefix\":\"\"}}");
        assertEquals(36L, parse(out.getFieldsJson()).get("seq_no").get("seqStart").asLong(),
                "起始值必须是 MAX+1，否则重复执行会撞唯一键");
        assertTrue(out.getWarnings().isEmpty());
    }

    @Test
    void emptyTableFallsBackToOneWithWarning() throws Exception {
        stubMaxNull();
        var out = resolver.refresh(conn, "perf_order",
                "{\"seq_no\":{\"type\":\"seq\",\"seqPrefix\":\"\"}}");
        assertEquals(SeqStartResolver.FALLBACK_START,
                parse(out.getFieldsJson()).get("seq_no").get("seqStart").asLong());
        assertTrue(out.getWarnings().stream().anyMatch(w -> w.contains("探测失败")),
                "回退必须告警，提醒用户可能撞唯一键: " + out.getWarnings());
    }

    @Test
    void prefixIsPreservedAndStartIsRefreshed() throws Exception {
        stubMax(99L);
        var out = resolver.refresh(conn, "perf_order",
                "{\"coupon_no\":{\"type\":\"seq\",\"seqPrefix\":\"C\"}}");
        JsonNode rule = parse(out.getFieldsJson()).get("coupon_no");
        assertEquals("C", rule.get("seqPrefix").asText());
        assertEquals(100L, rule.get("seqStart").asLong());
    }

    @Test
    void nonSeqRulesAreLeftUntouched() throws Exception {
        String fields = "{\"status\":{\"type\":\"enum\",\"values\":[1,2]}}";
        var out = resolver.refresh(conn, "perf_order", fields);
        assertEquals(fields, out.getFieldsJson());
    }

    @Test
    void mixedRulesOnlyRefreshSeqOnes() throws Exception {
        stubMax(5L);
        var out = resolver.refresh(conn, "perf_order",
                "{\"status\":{\"type\":\"enum\",\"values\":[1]},"
                        + "\"seq_no\":{\"type\":\"seq\",\"seqPrefix\":\"\"}}");
        JsonNode root = parse(out.getFieldsJson());
        assertEquals("enum", root.get("status").get("type").asText());
        assertEquals(6L, root.get("seq_no").get("seqStart").asLong());
    }

    @Test
    void blankOrNullInputIsReturnedAsIs() {
        assertEquals(null, resolver.refresh(conn, "perf_order", null).getFieldsJson());
        assertEquals("  ", resolver.refresh(conn, "perf_order", "  ").getFieldsJson());
    }

    @Test
    void nullConnectionIsNoOp() {
        var out = resolver.refresh(null, "perf_order",
                "{\"seq_no\":{\"type\":\"seq\",\"seqPrefix\":\"\"}}");
        assertTrue(out.getFieldsJson().contains("seq"));
    }

    @Test
    void brokenJsonProducesWarningNotException() {
        var out = resolver.refresh(conn, "perf_order", "not json");
        assertTrue(out.getWarnings().stream().anyMatch(w -> w.contains("刷新失败")),
                "解析失败必须告警: " + out.getWarnings());
    }

    @Test
    void queryFailureFallsBackToNextStrategy() throws Exception {
        when(conn.prepareStatement(anyString())).thenThrow(new java.sql.SQLException("boom"));
        var out = resolver.refresh(conn, "perf_order",
                "{\"coupon_no\":{\"type\":\"seq\",\"seqPrefix\":\"C\"}}");
        assertEquals(SeqStartResolver.FALLBACK_START,
                parse(out.getFieldsJson()).get("coupon_no").get("seqStart").asLong());
        assertTrue(out.getWarnings().stream().anyMatch(w -> w.contains("探测失败")));
    }

    @Test
    void stringMaxValueIsParsed() throws Exception {
        when(conn.prepareStatement(anyString())).thenReturn(ps);
        when(ps.executeQuery()).thenReturn(rs);
        when(rs.next()).thenReturn(true);
        when(rs.getObject(1)).thenReturn("250");
        assertEquals(251L, resolver.detectStart(conn, "perf_order", "coupon_no", "C"));
    }

    @Test
    void nonNumericStringYieldsZeroSoCallerFallsBack() throws Exception {
        when(conn.prepareStatement(anyString())).thenReturn(ps);
        when(ps.executeQuery()).thenReturn(rs);
        when(rs.next()).thenReturn(true);
        when(rs.getObject(1)).thenReturn("NOT_A_NUMBER");
        assertEquals(0, resolver.detectStart(conn, "perf_order", "coupon_no", "C"));
    }
}
