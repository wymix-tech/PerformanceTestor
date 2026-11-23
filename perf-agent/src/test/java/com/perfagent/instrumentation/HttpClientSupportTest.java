package com.perfagent.instrumentation;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.HttpReporter;
import com.perfagent.reporter.MetricsReporter;
import com.perfagent.trace.TraceContext;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

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
}
