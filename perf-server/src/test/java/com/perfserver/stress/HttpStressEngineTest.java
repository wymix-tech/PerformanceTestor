package com.perfserver.stress;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * HttpStressEngine 端到端行为测试（JDK 内置 HttpServer）：
 * warmup 期请求不计入统计、采样仅覆盖正式期、无 warmup 时行为兼容。
 */
class HttpStressEngineTest {

    private HttpServer server;
    private final AtomicLong hitCount = new AtomicLong();
    private final CountDownLatch firstHit = new CountDownLatch(1);

    @BeforeEach
    void setUp() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/ping", exchange -> {
            hitCount.incrementAndGet();
            firstHit.countDown();
            byte[] body = "pong".getBytes();
            exchange.sendResponseHeaders(200, body.length);
            try (OutputStream os = exchange.getResponseBody()) {
                os.write(body);
            }
        });
        server.start();
    }

    @AfterEach
    void tearDown() {
        server.stop(0);
    }

    private String url() {
        return "http://127.0.0.1:" + server.getAddress().getPort() + "/ping";
    }

    @Test
    void warmupRequestsAreExcludedFromFormalStats() throws Exception {
        HttpStressEngine engine = new HttpStressEngine();
        StressTaskRequest req = StressTaskRequest.single(url(), 2, 2);
        req.setWarmupSec(1);
        req.setSampleIntervalSec(1);

        StressTaskResult result = engine.run(req, null);

        // 服务端确实收到请求（warmup + formal 都发压）
        assertTrue(firstHit.await(5, TimeUnit.SECONDS), "压测未产生任何请求");
        assertTrue(hitCount.get() >= result.getTotalRequests(),
                "服务端命中数(" + hitCount.get() + ") 应不少于正式统计数(" + result.getTotalRequests() + ")");
        // 正式期（2s × 2 并发本地回环）应有可观测请求量
        assertTrue(result.getTotalRequests() >= 4, "正式统计请求数异常偏小: " + result.getTotalRequests());
        assertEquals(1, result.getWarmupSec());
        // 错误率应为 0（本地 200 响应）
        assertEquals(0.0, result.getErrorRate(), 0.0);
        // 采样点存在且非空
        assertNotNull(result.getSamples());
        assertTrue(result.getSamples().size() >= 1, "正式期应产生采样点");
        // QPS 为正数
        assertTrue(result.getQps() > 0);
    }

    @Test
    void noWarmupRunsImmediatelyWithStats() throws Exception {
        HttpStressEngine engine = new HttpStressEngine();
        StressTaskRequest req = StressTaskRequest.single(url(), 2, 1);
        req.setWarmupSec(0);
        req.setSampleIntervalSec(1);

        StressTaskResult result = engine.run(req, null);
        assertTrue(result.getTotalRequests() >= 2, "无 warmup 模式应立即统计: " + result.getTotalRequests());
        assertEquals(0, result.getWarmupSec());
    }

    @Test
    void httpErrorCountsAsError() throws Exception {
        server.createContext("/err", exchange -> {
            byte[] body = "boom".getBytes();
            exchange.sendResponseHeaders(500, body.length);
            try (OutputStream os = exchange.getResponseBody()) {
                os.write(body);
            }
        });
        HttpStressEngine engine = new HttpStressEngine();
        StressTaskRequest req = new StressTaskRequest();
        req.setTargetUrl("http://127.0.0.1:" + server.getAddress().getPort() + "/err");
        req.setConcurrency(1);
        req.setDurationSec(1);
        req.setWarmupSec(0);
        req.setSampleIntervalSec(1);

        StressTaskResult result = engine.run(req, null);
        assertTrue(result.getTotalRequests() >= 1);
        assertTrue(result.getErrorCount() >= 1, "HTTP 500 应计为错误");
        assertTrue(result.getErrorRate() > 0);
    }
}
