package com.perfserver.stress;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 多接口场景端到端压测：权重配比、登录态提取与传递、分步骤统计。
 *
 * <p>用本地 HTTP 桩服务，不依赖外部网络。
 */
class StressScenarioTest {

    private static HttpServer server;
    private static String base;

    @BeforeAll
    static void startServer() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/login", ex -> {
            respond(ex, 200, "{\"data\":{\"token\":\"tk-" + System.nanoTime() + "\"}}");
        });
        server.createContext("/order", ex -> {
            String auth = ex.getRequestHeaders().getFirst("Authorization");
            // 携带有效 token 视为成功，否则 401：验证变量确实在步骤间传递
            respond(ex, auth != null && auth.startsWith("Bearer tk-") ? 200 : 401, "{\"ok\":true}");
        });
        server.createContext("/home", ex -> respond(ex, 200, "{\"ok\":true}"));
        server.start();
        base = "http://127.0.0.1:" + server.getAddress().getPort();
    }

    @AfterAll
    static void stopServer() {
        server.stop(0);
    }

    private static void respond(com.sun.net.httpserver.HttpExchange ex, int code, String body)
            throws java.io.IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json");
        ex.sendResponseHeaders(code, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    @Test
    void runsWeightedScenarioWithVariablePassing() {
        StressTaskRequest request = new StressTaskRequest();
        request.setConcurrency(4);
        request.setDurationSec(3);
        request.setWarmupSec(0);
        request.setSampleIntervalSec(1);

        StressTaskRequest.ScenarioStep login = new StressTaskRequest.ScenarioStep();
        login.setName("login");
        login.setUrl(base + "/login");
        login.setMethod("GET");
        login.setWeight(2);
        StressTaskRequest.VarExtract ex = new StressTaskRequest.VarExtract();
        ex.setVar("token");
        ex.setPath("data.token");
        login.setExtract(List.of(ex));

        StressTaskRequest.ScenarioStep order = new StressTaskRequest.ScenarioStep();
        order.setName("order");
        order.setUrl(base + "/order");
        order.setMethod("GET");
        order.setWeight(8);
        order.setHeaders(Map.of("Authorization", "Bearer {{token}}"));

        request.setScenario(List.of(login, order));

        assertTrue(request.isScenarioMode());
        StressTaskResult result = new HttpStressEngine().run(request, null);

        assertNotNull(result.getStepStats(), "场景模式应产出分步骤统计");
        assertEquals(2, result.getStepStats().size());
        assertTrue(result.getTotalRequests() > 0, "应实际发出请求");

        StressStatistician.StepStats loginStats = byName(result.getStepStats(), "login");
        StressStatistician.StepStats orderStats = byName(result.getStepStats(), "order");
        // login 必定成功（桩服务恒返回 200 且带 token）
        assertEquals(0, loginStats.getErrorCount());
        // 权重 2:8 → order 请求量显著多于 login
        assertTrue(orderStats.getTotalRequests() > loginStats.getTotalRequests(),
                "order 权重更高，请求数应更多，实际 login=" + loginStats.getTotalRequests()
                        + " order=" + orderStats.getTotalRequests());
        // 变量传递生效：order 不应全量 401（token 已由 login 提取并注入）
        double orderErrorRate = orderStats.getErrorRate();
        assertTrue(orderErrorRate < 100,
                "order 错误率应低于 100%，说明登录态提取与传递生效，实际 " + orderErrorRate + "%");
        // 汇总口径不变
        assertEquals(loginStats.getTotalRequests() + orderStats.getTotalRequests(),
                result.getTotalRequests());
    }

    @Test
    void singleInterfaceModeHasNoStepStats() {
        StressTaskRequest request = StressTaskRequest.single(base + "/home", 2, 2);
        request.setWarmupSec(0);
        request.setSampleIntervalSec(1);

        StressTaskResult result = new HttpStressEngine().run(request, null);

        assertFalse(request.isScenarioMode());
        assertTrue(result.getStepStats().isEmpty(), "单接口模式不应产出分步骤统计");
        assertTrue(result.getTotalRequests() > 0);
    }

    @Test
    void scenarioStatsAreSortedByName() {
        StressTaskRequest request = new StressTaskRequest();
        request.setConcurrency(2);
        request.setDurationSec(2);
        request.setWarmupSec(0);
        request.setScenario(List.of(step("zeta", base + "/home", 1), step("alpha", base + "/home", 1)));

        StressTaskResult result = new HttpStressEngine().run(request, null);
        List<String> names = new ArrayList<>();
        result.getStepStats().forEach(s -> names.add(s.getStepName()));
        assertEquals(List.of("alpha", "zeta"), names);
    }

    private static StressTaskRequest.ScenarioStep step(String name, String url, int weight) {
        StressTaskRequest.ScenarioStep s = new StressTaskRequest.ScenarioStep();
        s.setName(name);
        s.setUrl(url);
        s.setWeight(weight);
        return s;
    }

    private static StressStatistician.StepStats byName(List<StressStatistician.StepStats> stats, String name) {
        return stats.stream().filter(s -> s.getStepName().equals(name)).findFirst().orElseThrow();
    }
}
