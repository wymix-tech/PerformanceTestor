package com.perfagent.instrumentation;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.HttpReporter;
import com.perfagent.reporter.MetricsReporter;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 中间件拦截单测：命令文本渲染（各参数类型/截断/兜底）、阈值判定、上报链路与配置解析。
 *
 * <p>上报链路用 HttpReporter 替身捕获 body，验证 slowCommand 通道与字段完整性。
 */
class MiddlewareSupportTest {

    /** 记录型 HttpReporter 替身 */
    private static final class RecordingHttp extends HttpReporter {
        final List<String> posts = new CopyOnWriteArrayList<>();

        @Override
        public PostResult postJson(String url, String body) {
            posts.add(body);
            return new PostResult(true, 200, "");
        }
    }

    // ---------------- 命令渲染 ----------------

    private static Method method(String name) {
        return name == null ? null : Holder.method(name);
    }

    /** 提供测试用 Method 对象（非反射取被测方法，仅为渲染入参） */
    static class Holder {
        static Method method(String name) {
            for (Method m : Holder.class.getDeclaredMethods()) {
                if (m.getName().equals(name)) {
                    return m;
                }
            }
            throw new IllegalArgumentException("no method " + name);
        }

        void get() {
        }

        void basicPublish() {
        }

        void performRequest() {
        }
    }

    @Test
    void describeRendersMethodNameAndArgs() {
        String cmd = MiddlewareSupport.describe(method("get"), new Object[]{"user:123"});
        assertEquals("get user:123", cmd);
    }

    @Test
    void describeRendersNumbersBooleansAndNulls() {
        String cmd = MiddlewareSupport.describe(method("get"),
                new Object[]{"key", 42, true, null, 'c'});
        assertEquals("get key 42 true null c", cmd);
    }

    @Test
    void describeRendersByteArrayWithSizePrefixAndTruncates() {
        byte[] body = "{\"orderId\":1234567890}".getBytes(StandardCharsets.UTF_8);
        String cmd = MiddlewareSupport.describe(method("basicPublish"),
                new Object[]{"order.exchange", "order.created", null, body});
        assertTrue(cmd.startsWith("basicPublish order.exchange order.created null <"), cmd);
        assertTrue(cmd.contains("<" + body.length + "B>"), "应带字节数前缀: " + cmd);
        assertTrue(cmd.contains("\"orderId\""), "应解码内容: " + cmd);
    }

    @Test
    void describeTruncatesLargeBytes() {
        byte[] big = new byte[4096];
        java.util.Arrays.fill(big, (byte) 'x');
        String cmd = MiddlewareSupport.describe(method("basicPublish"), new Object[]{big});
        assertTrue(cmd.contains("<4096B>"), "应记录原始长度: " + cmd);
        assertTrue(cmd.length() <= 1000 + 10, "命令总长应被截断: " + cmd.length());
    }

    @Test
    void describeRendersCollectionsAndArrays() {
        String cmd = MiddlewareSupport.describe(method("get"),
                new Object[]{List.of("a", "b", "c"), new String[]{"x", "y"}});
        assertEquals("get [a,b,c] [x,y]", cmd);
    }

    @Test
    void describeLimitsArgsCount() {
        String cmd = MiddlewareSupport.describe(method("get"),
                new Object[]{"a", "b", "c", "d", "e", "f", "g", "h"});
        assertTrue(cmd.endsWith("(+2 more)"), "超出 6 个参数应记省略: " + cmd);
    }

    @Test
    void describeFallsBackToSimpleClassNameForComplexObjects() {
        String cmd = MiddlewareSupport.describe(method("performRequest"),
                new Object[]{new Object()});
        assertEquals("performRequest <Object>", cmd, "复杂对象不调 toString，仅记类型避免大序列化开销");
    }

    @Test
    void describeHandlesNullMethodAndNullArgs() {
        assertEquals("<unknown>", MiddlewareSupport.describe(null, null));
        assertEquals("get", MiddlewareSupport.describe(method("get"), null));
    }

    @Test
    void describeNeverThrowsOnHostileToString() {
        Object evil = new Object() {
            @Override
            public String toString() {
                throw new RuntimeException("boom");
            }
        };
        // 复杂对象走 getClass().getSimpleName()，不触发 toString
        assertTrue(MiddlewareSupport.describe(method("get"), new Object[]{evil}).contains("get "));
    }

    // ---------------- 阈值判定与上报 ----------------

    @Test
    void reportSkipsBelowThresholdAndReportsAbove() {
        AgentConfig config = new AgentConfig();
        config.appName = "demo-app";
        config.slowCommandThresholdMs = 100;
        RecordingHttp http = new RecordingHttp();
        MetricsReporter reporter = new MetricsReporter(config, http);
        reporter.start();
        MiddlewareSupport.init(config, reporter);
        try {
            Object fast = MiddlewareSupport.enter(System.nanoTime(), new Object[]{"k"});
            MiddlewareSupport.report(MiddlewareSupport.TYPE_REDIS, fast, method("get"));
            assertEquals(0, reporter.pendingSlowCommandCount(), "未超阈值不上报");

            Object slow = MiddlewareSupport.enter(System.nanoTime() - 150_000_000L, new Object[]{"k"});
            MiddlewareSupport.report(MiddlewareSupport.TYPE_REDIS, slow, method("get"));
            assertEquals(1, reporter.pendingSlowCommandCount(), "超阈值应入队");
        } finally {
            reporter.stop();
            MiddlewareSupport.reporter = null;
        }

        String body = http.posts.stream().filter(b -> b.contains("\"type\":\"slowCommand\""))
                .findFirst().orElse(null);
        assertNotNull(body, "应上报 slowCommand 批次: " + http.posts);
        assertTrue(body.contains("\"type\":\"redis\""), "body 应含中间件类型: " + body);
        assertTrue(body.contains("get k"), "body 应含命令文本: " + body);
    }

    @Test
    void reportIgnoresNonEnterObjectAndNullReporter() {
        MiddlewareSupport.reporter = null;
        MiddlewareSupport.report("redis", "not-an-enter", method("get"));
        MiddlewareSupport.report("redis", null, method("get"));
        // 无上报器时静默返回，不抛异常
    }

    // ---------------- 配置 ----------------

    @Test
    void configDefaultsMiddlewareDisabled() {
        AgentConfig cfg = new AgentConfig();
        assertFalse(cfg.middlewareEnabled, "中间件拦截默认关闭以控制常态开销");
        assertEquals(100, cfg.slowCommandThresholdMs, "慢命令默认阈值 100ms");
    }

    @Test
    void configParsesMiddlewareArgs() {
        AgentConfig cfg = AgentConfig.fromArgs(
                "appName=demo,middlewareEnabled=true,slowCommandThresholdMs=25");
        assertTrue(cfg.middlewareEnabled);
        assertEquals(25, cfg.slowCommandThresholdMs);
        assertEquals("demo", cfg.appName);
    }

    @Test
    void typeConstantsMatchPlatformContract() {
        assertEquals("redis", MiddlewareSupport.TYPE_REDIS);
        assertEquals("es", MiddlewareSupport.TYPE_ES);
        assertEquals("mq", MiddlewareSupport.TYPE_MQ);
    }
}
