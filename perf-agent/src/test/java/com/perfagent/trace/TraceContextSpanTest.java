package com.perfagent.trace;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.HttpReporter;
import com.perfagent.reporter.MetricsReporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * P2-2 W3C traceparent 跨服务串联测试：Filter 根入口继承/生成 traceId 与 span、
 * parentSpanId 记录、currentTraceparent() 出口格式、快照透传。
 */
class TraceContextSpanTest {

    /** 记录型替身：捕获提交的 span 字段 */
    private static final class RecordingReporter extends MetricsReporter {
        final List<String> spanIds = new CopyOnWriteArrayList<>();
        final List<String> parentSpanIds = new CopyOnWriteArrayList<>();

        RecordingReporter() {
            super(new AgentConfig(), new HttpReporter());
        }

        @Override
        public void reportTrace(String traceId, String app, TraceNode root, int nodeCount,
                                long startTimeMs, String stressTaskId,
                                String spanId, String parentSpanId) {
            spanIds.add(spanId);
            parentSpanIds.add(parentSpanId);
        }
    }

    /** 测试用最小请求替身：仅提供 getHeader(String) 方法签名 */
    static class FakeServletRequest {
        String traceparent;

        public String getHeader(String name) {
            return traceparent;
        }
    }

    private RecordingReporter reporter;

    @BeforeEach
    void setUp() {
        reporter = new RecordingReporter();
        TraceContext.init("test-app", new TraceSampler(1.0, 0), reporter);
    }

    @AfterEach
    void tearDown() {
        TraceContext.cleanup();
    }

    private static boolean isLowerHex(String s) {
        for (char c : s.toCharArray()) {
            if ((c < '0' || c > '9') && (c < 'a' || c > 'f')) {
                return false;
            }
        }
        return true;
    }

    @Test
    void noTraceparentGeneratesW3cIdentity() {
        assertTrue(TraceContext.beginFilterRoot(new FakeServletRequest()));
        String traceId = TraceContext.currentTraceId();
        assertEquals(32, traceId.length());
        assertTrue(isLowerHex(traceId));
        String spanId = TraceContext.currentSpanId();
        assertEquals(16, spanId.length());
        assertTrue(isLowerHex(spanId));
        assertNull(TraceContext.currentParentSpanId());
        assertEquals("00-" + traceId + "-" + spanId + "-01", TraceContext.currentTraceparent());
    }

    @Test
    void validTraceparentIsInheritedWithOwnSpan() {
        FakeServletRequest req = new FakeServletRequest();
        req.traceparent = "00-463ac810f4b34f4d9381a4b392d3c944-1d24ab9093ba4f11-01";
        assertTrue(TraceContext.beginFilterRoot(req));
        assertEquals("463ac810f4b34f4d9381a4b392d3c944", TraceContext.currentTraceId());
        assertEquals("1d24ab9093ba4f11", TraceContext.currentParentSpanId());
        String spanId = TraceContext.currentSpanId();
        assertEquals(16, spanId.length());
        assertEquals("00-463ac810f4b34f4d9381a4b392d3c944-" + spanId + "-01",
                TraceContext.currentTraceparent());
    }

    @Test
    void invalidTraceparentIsIgnored() {
        String[] bad = {
                null,
                "",
                "00-463ac810f4b34f4d9381a4b392d3c944-1d24ab9093ba4f11",
                "ff-463ac810f4b34f4d9381a4b392d3c944-1d24ab9093ba4f11-01",
                "00-00000000000000000000000000000000-1d24ab9093ba4f11-01",
                "00-463ac810f4b34f4d9381a4b392d3c944-0000000000000000-01",
                "00-463ac810f4b34f4d9381a4b392D3c944-1d24ab9093ba4f11-01",
        };
        for (String tp : bad) {
            assertNull(TraceContext.parseTraceparent(tp), "应判非法: " + tp);
        }
        assertNotNull(TraceContext.parseTraceparent(
                "00-463ac810f4b34f4d9381a4b392d3c944-1d24ab9093ba4f11-01"));
    }

    @Test
    void submittedTraceCarriesSpanFields() {
        FakeServletRequest req = new FakeServletRequest();
        req.traceparent = "00-463ac810f4b34f4d9381a4b392d3c944-1d24ab9093ba4f11-01";
        TraceContext.beginFilterRoot(req);
        TraceContext.begin("com.demo.Api#handle");
        TraceContext.end(System.nanoTime());
        assertEquals("1d24ab9093ba4f11", reporter.parentSpanIds.get(0));
        assertEquals(16, reporter.spanIds.get(0).length());
        TraceContext.endFilterRoot();
    }

    @Test
    void successiveRequestsGenerateDistinctIds() {
        TraceContext.beginFilterRoot(new FakeServletRequest());
        String firstTrace = TraceContext.currentTraceId();
        String firstSpan = TraceContext.currentSpanId();
        TraceContext.endFilterRoot();
        TraceContext.beginFilterRoot(new FakeServletRequest());
        assertNotEquals(firstTrace, TraceContext.currentTraceId());
        assertNotEquals(firstSpan, TraceContext.currentSpanId());
        TraceContext.endFilterRoot();
    }
}
