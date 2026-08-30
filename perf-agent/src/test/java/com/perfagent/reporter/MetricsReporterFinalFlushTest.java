package com.perfagent.reporter;

import com.perfagent.config.AgentConfig;
import com.perfagent.trace.TraceNode;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * MetricsReporter 停机冲刷测试：stop() 应尽力上报队列中剩余数据
 * （修复旧实现 stop 仅 shutdownNow、注释声称冲刷但数据静默丢弃的缺陷）。
 */
class MetricsReporterFinalFlushTest {

    /** 记录型 HttpReporter 替身 */
    private static final class RecordingHttp extends HttpReporter {
        final List<String> posts = new CopyOnWriteArrayList<>();

        @Override
        public PostResult postJson(String url, String body) {
            posts.add(body);
            return new PostResult(true, 200, "");
        }
    }

    @Test
    void stopFlushesPendingTraces() {
        AgentConfig config = new AgentConfig();
        config.reportUrl = "http://127.0.0.1:1/api/agent/report"; // 不实际发送（替身接管）
        RecordingHttp http = new RecordingHttp();
        MetricsReporter reporter = new MetricsReporter(config, http);
        reporter.start();

        // 立即入队（调度器首次 flush 在 1s 后，stop 前不会触发定时路径）
        TraceNode root = TraceNode.of("com.demo.controller.UserController#list", 12.5);
        reporter.reportTrace("trace-flush-test", "demo-app", root, 1, System.currentTimeMillis());

        reporter.stop(); // 应冲刷剩余数据

        boolean flushed = http.posts.stream().anyMatch(b -> b.contains("trace-flush-test"));
        assertTrue(flushed, "stop() 应尽力冲刷队列中剩余 trace（旧实现静默丢弃）");
    }

    @Test
    void stopIsIdempotentAndSafeOnEmptyQueues() {
        AgentConfig config = new AgentConfig();
        MetricsReporter reporter = new MetricsReporter(config, new RecordingHttp());
        reporter.start();
        reporter.reportJvm("demo-app", new java.util.HashMap<>());
        reporter.reportJvm("demo-app", new java.util.HashMap<>());
        reporter.stop();
        reporter.stop(); // 二次 stop 不抛异常
        assertTrue(reporter.pendingJvmCount() == 0, "停机后 jvm 队列应已清空");
    }
}
