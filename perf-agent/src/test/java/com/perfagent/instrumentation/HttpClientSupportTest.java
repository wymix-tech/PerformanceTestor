package com.perfagent.instrumentation;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.HttpReporter;
import com.perfagent.reporter.MetricsReporter;
import com.perfagent.trace.TraceContext;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Field;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * HttpClientSupport 反射注入测试（P2-2）：用最小替身模拟三类客户端 API 形态，
 * 验证 perf 上下文内注入、已有 traceparent 去重、无上下文零改动。
 */
class HttpClientSupportTest {

    /** Apache 形态替身：addHeader / getFirstHeader */
    static class FakeHttpUriRequest {
        String traceparent;
        String stressTaskId;

        public void addHeader(String name, String value) {
            if ("traceparent".equals(name)) {
                traceparent = value;
            } else if ("X-Perf-Stress-Task-Id".equals(name)) {
                stressTaskId = value;
            }
        }

        public Object getFirstHeader(String name) {
            return "traceparent".equals(name) && traceparent != null
                    ? new Object() : null;
        }
    }

    /** OkHttp/JDK 形态替身：newBuilder().setHeader(...).build() */
    static class FakeRequest {
        String traceparent;
        String stressTaskId;

        public Builder newBuilder() {
            return new Builder(this);
        }

        static class Builder {
            private final FakeRequest src;
            private String tp;
            private String task;

            Builder(FakeRequest src) {
                this.src = src;
            }

            public Builder setHeader(String name, String value) {
                if ("traceparent".equals(name)) {
                    tp = value;
                } else if ("X-Perf-Stress-Task-Id".equals(name)) {
                    task = value;
                }
                return this;
            }

            public FakeRequest build() {
                FakeRequest r = new FakeRequest();
                r.traceparent = tp != null ? tp : src.traceparent;
                r.stressTaskId = task != null ? task : src.stressTaskId;
                return r;
            }
        }
    }

    /** 携带染色标识的请求替身（getHeader 形态，与 Servlet 一致） */
    static class StressRequest {
        public String getHeader(String name) {
            return "X-Perf-Stress-Task-Id".equals(name) ? "42" : null;
        }
    }

    /** 未采样压测流量替身：染色任务 ID 为 88（Fix-2 回归场景） */
    static class UnsampledStressRequest {
        public String getHeader(String name) {
            return "X-Perf-Stress-Task-Id".equals(name) ? "88" : null;
        }
    }

    /** 严格 Apache 替身：header 值为 null 直接抛 NPE（对齐 OkHttp/HttpHeaders 严格语义） */
    static class StrictApacheRequest {
        String traceparent;
        String stressTaskId;

        public void addHeader(String name, String value) {
            java.util.Objects.requireNonNull(value, name);
            if ("traceparent".equals(name)) {
                traceparent = value;
            } else if ("X-Perf-Stress-Task-Id".equals(name)) {
                stressTaskId = value;
            }
        }

        public Object getFirstHeader(String name) {
            return "traceparent".equals(name) && traceparent != null ? new Object() : null;
        }
    }

    /** 严格 OkHttp/JDK 替身：setHeader null 值抛 NPE */
    static class StrictRequest {
        String traceparent;
        String stressTaskId;

        public Builder newBuilder() {
            return new Builder(this);
        }

        static class Builder {
            private final StrictRequest src;
            private String tp;
            private String task;

            Builder(StrictRequest src) {
                this.src = src;
            }

            public Builder setHeader(String name, String value) {
                java.util.Objects.requireNonNull(value, name);
                if ("traceparent".equals(name)) {
                    tp = value;
                } else if ("X-Perf-Stress-Task-Id".equals(name)) {
                    task = value;
                }
                return this;
            }

            public StrictRequest build() {
                StrictRequest r = new StrictRequest();
                r.traceparent = tp != null ? tp : src.traceparent;
                r.stressTaskId = task != null ? task : src.stressTaskId;
                return r;
            }
        }
    }

    /** 严格 Spring 替身：HttpHeaders.set null 值抛 NPE */
    static class StrictSpringRequest {
        final Map<String, String> headers = new LinkedHashMap<>();

        public StrictHeaders getHeaders() {
            return new StrictHeaders();
        }

        class StrictHeaders {
            public void set(String name, String value) {
                java.util.Objects.requireNonNull(value, name);
                headers.put(name, value);
            }
        }
    }

    private final AgentConfig config = new AgentConfig();

    @BeforeEach
    void setUp() {
        TraceContext.init("test-app",
                new com.perfagent.trace.TraceSampler(1.0, 0),
                new MetricsReporter(config, new HttpReporter()));
    }

    @AfterEach
    void tearDown() {
        TraceContext.cleanup();
    }

    private void enterPerfContext(boolean withStress) {
        TraceContext.beginFilterRoot(withStress ? new StressRequest() : null);
    }

    @Test
    void injectApacheAddsTraceparentAndStressHeader() {
        enterPerfContext(true);
        FakeHttpUriRequest req = new FakeHttpUriRequest();
        HttpClientSupport.injectApache(req);
        assertTrue(req.traceparent != null && req.traceparent.startsWith("00-"));
        assertEquals("42", req.stressTaskId);
    }

    @Test
    void injectApacheSkipsWhenTraceparentPresent() {
        enterPerfContext(false);
        FakeHttpUriRequest req = new FakeHttpUriRequest();
        req.traceparent = "00-existing";
        HttpClientSupport.injectApache(req);
        assertEquals("00-existing", req.traceparent);
        assertNull(req.stressTaskId);
    }

    @Test
    void injectRequestRebuildsWithHeaders() {
        enterPerfContext(true);
        FakeRequest req = new FakeRequest();
        Object out = HttpClientSupport.injectRequest(req);
        assertTrue(out instanceof FakeRequest);
        assertNotSame(req, out);
        FakeRequest rebuilt = (FakeRequest) out;
        assertTrue(rebuilt.traceparent != null && rebuilt.traceparent.startsWith("00-"));
        assertEquals("42", rebuilt.stressTaskId);
    }

    @Test
    void noContextMeansNoInjection() {
        FakeHttpUriRequest apache = new FakeHttpUriRequest();
        HttpClientSupport.injectApache(apache);
        assertNull(apache.traceparent);
        FakeRequest generic = new FakeRequest();
        Object out = HttpClientSupport.injectRequest(generic);
        assertNull(((FakeRequest) out).traceparent);
    }

    private static void assertNotSame(Object a, Object b) {
        org.junit.jupiter.api.Assertions.assertNotSame(a, b);
    }

    /**
     * 构造「未采样压测流量」极端上下文（Fix-2）：sampleRate=0.0 进入 Filter 根
     * （染色 ID "88" 被无条件捕获），再经反射清空 traceId/spanId，使
     * {@code currentTraceparent() == null} 而 {@code currentStressTaskId() == "88"}。
     */
    private void forceUnsampledStressContext() throws Exception {
        TraceContext.init("test-app", new com.perfagent.trace.TraceSampler(0.0, 0),
                new MetricsReporter(config, new HttpReporter()));
        TraceContext.beginFilterRoot(new UnsampledStressRequest());
        Field ctxField = TraceContext.class.getDeclaredField("CTX");
        ctxField.setAccessible(true);
        Object ctx = ((ThreadLocal<?>) ctxField.get(null)).get();
        for (String name : new String[]{"traceId", "spanId"}) {
            Field f = ctx.getClass().getDeclaredField(name);
            f.setAccessible(true);
            f.set(ctx, null);
        }
        assertNull(TraceContext.currentTraceparent());
        assertEquals("88", TraceContext.currentStressTaskId());
    }

    @Test
    void unsampledStressTrafficInjectsStressHeaderWithoutTraceparent() throws Exception {
        // 未采样压测流量：traceparent 为 null 不得写入（严格替身遇 null 值抛 NPE），
        // X-Perf-Stress-Task-Id 必须独立照常透传——跨服务掉染色回归防线
        forceUnsampledStressContext();

        StrictApacheRequest apache = new StrictApacheRequest();
        HttpClientSupport.injectApache(apache);
        assertNull(apache.traceparent, "traceparent 为 null 不得写入");
        assertEquals("88", apache.stressTaskId, "染色头必须照常透传");

        StrictRequest generic = new StrictRequest();
        StrictRequest rebuilt = (StrictRequest) HttpClientSupport.injectRequest(generic);
        assertNull(rebuilt.traceparent, "traceparent 为 null 不得写入");
        assertEquals("88", rebuilt.stressTaskId, "染色头必须照常透传");

        StrictSpringRequest spring = new StrictSpringRequest();
        HttpClientSupport.injectSpring(spring);
        assertNull(spring.headers.get("traceparent"), "traceparent 为 null 不得写入");
        assertEquals("88", spring.headers.get("X-Perf-Stress-Task-Id"), "染色头必须照常透传");
    }
}
