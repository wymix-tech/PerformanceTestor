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
    /**
     * /order 侧观测计数，供「上下文引用是否真的生效」断言使用。
     *
     * <p>用计数器而非收集样本列表：场景压测每秒可达数千次请求，全量留存会白白吃掉
     * 几十 MB 堆；而若只留存前 N 条，warmup 期未命中的样本会把缓冲占满，
     * 导致正式期的命中样本根本进不来——断言会假阴性。计数器无此偏差。
     */
    private static final java.util.concurrent.atomic.AtomicInteger tokenHits = new java.util.concurrent.atomic.AtomicInteger();
    private static final java.util.concurrent.atomic.AtomicInteger traceHits = new java.util.concurrent.atomic.AtomicInteger();

    @BeforeAll
    static void startServer() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/login", ex -> {
            // 下发 X-Request-Id 响应头：供下游 {{p1.header.X-Request-Id}} 引用
            ex.getResponseHeaders().add("X-Request-Id", "trace-1");
            respond(ex, 200, "{\"data\":{\"token\":\"tk-" + System.nanoTime() + "\",\"userId\":\"u-9527\"}}");
        });
        server.createContext("/order", ex -> {
            String auth = ex.getRequestHeaders().getFirst("Authorization");
            String trace = ex.getRequestHeaders().getFirst("X-Trace-Id");
            // 命中判定：未命中时占位符会原样出现（"Bearer {{p1.result.data.token}}"），
            // 不会匹配 "Bearer tk-" / "trace-" 前缀
            if (auth != null && auth.startsWith("Bearer tk-")) {
                tokenHits.incrementAndGet();
            }
            if (trace != null && trace.startsWith("trace-") && !trace.contains("{{")) {
                traceHits.incrementAndGet();
            }
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
        // 时长取 5s：本用例要验证 2:8 权重的流量配比，需要足够样本量才能体现
        // 差异；CI 机器负载高时短窗口样本过少，比例断言会随机失败
        request.setDurationSec(5);
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
        // 样本量下限：两步都要有足够请求，否则「权重配比」的比较不具统计意义
        // （如 login=1 order=1 时比较结果随机，会让本用例在慢机器上随机失败）
        assertTrue(result.getTotalRequests() >= 10, "应实际发出足量请求，实际 " + result.getTotalRequests());

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

    /**
     * 步骤上下文引用端到端：<b>零 extract 声明</b>，下游请求头直接引用上游响应体与响应头。
     *
     * <p>同时锁定【评审 P1】的口径——warmup 期必然出现「order 先于 login 执行」的
     * 未命中，这些未命中<b>不得</b>被正式期统计带走，故正式期 renderMiss 应为 0。
     */
    @Test
    void runsContextReferenceWithoutExtractDeclaration() {
        tokenHits.set(0);
        traceHits.set(0);
        StressTaskRequest request = new StressTaskRequest();
        request.setConcurrency(4);
        request.setDurationSec(3);
        // warmup 期用于建立上下文（warmup 同样写上下文池），正式期引用必然命中
        request.setWarmupSec(3);
        request.setSampleIntervalSec(2);

        StressTaskRequest.ScenarioStep login = new StressTaskRequest.ScenarioStep();
        login.setAlias("login");
        // 中文名：显式 alias 已声明，name 不兼作别名，故不产生「别名不合法」告警（日志降噪）
        login.setName("登录");
        login.setUrl(base + "/login");
        login.setMethod("GET");
        login.setWeight(3);
        // 不声明任何 extract：下游直接引用本步响应

        StressTaskRequest.ScenarioStep order = new StressTaskRequest.ScenarioStep();
        order.setName("order");
        order.setUrl(base + "/order?u={{login.result.data.userId}}");
        order.setMethod("GET");
        order.setWeight(7);
        order.setHeaders(Map.of(
                "Authorization", "Bearer {{p1.result.data.token}}",
                "X-Trace-Id", "{{p1.header.X-Request-Id}}"));

        request.setScenario(List.of(login, order));

        StressTaskResult result = new HttpStressEngine().run(request, null);

        // 上下文引用生效：下游请求头携带了上游响应体的 token 与上游响应头的 traceId
        assertTrue(tokenHits.get() > 0,
                "下游请求头应携带上游响应体里的 token（零 extract 声明），实际命中 " + tokenHits.get() + " 次");
        assertTrue(traceHits.get() > 0,
                "下游请求头应携带上游响应头 X-Request-Id，实际命中 " + traceHits.get() + " 次");

        StressStatistician.StepStats orderStats = byName(result.getStepStats(), "order");
        assertTrue(orderStats.getTotalRequests() > 0);
        assertEquals(0, orderStats.getRenderMiss(),
                "warmup 期产生的未命中不得计入正式期统计（P1：miss 必须按迭代取零）");
        assertTrue(result.getWarnings().isEmpty(),
                "合法配置不应产生告警，实际 " + result.getWarnings());
    }

    /** 步骤序号越界：启动期产生 warning，运行期保留原文、计入 renderMiss，但不中断压测 */
    @Test
    void outOfRangeStepReferenceProducesWarningAndMiss() {
        StressTaskRequest request = new StressTaskRequest();
        request.setConcurrency(2);
        request.setDurationSec(2);
        request.setWarmupSec(0);
        request.setSampleIntervalSec(1);

        StressTaskRequest.ScenarioStep home = new StressTaskRequest.ScenarioStep();
        home.setName("home");
        home.setUrl(base + "/home");
        home.setWeight(1);

        StressTaskRequest.ScenarioStep bad = new StressTaskRequest.ScenarioStep();
        bad.setName("bad");
        bad.setUrl(base + "/home?x={{p9.result.a}}");
        bad.setWeight(1);

        request.setScenario(List.of(home, bad));

        StressTaskResult result = new HttpStressEngine().run(request, null);

        assertTrue(result.getWarnings().stream().anyMatch(w -> w.contains("p9")),
                "越界序号应产生配置告警，实际 " + result.getWarnings());
        StressStatistician.StepStats badStats = byName(result.getStepStats(), "bad");
        assertTrue(badStats.getTotalRequests() > 0, "未命中不得中断压测");
        // warmupSec=0，全程计入统计；p9 是确定的 pN 引用但越界 → 每次渲染都计一次未命中
        assertEquals(badStats.getTotalRequests(), badStats.getRenderMiss(),
                "越界的 pN 引用属确定的上下文引用未命中，应逐次计入 renderMiss");
        // 【降级行为】未命中保留原文后 URL 含非法字符 '{' '}'，URI.create 必然抛异常。
        // 引擎必须捕获它、计一次错误并继续下一迭代，而不是让 worker 线程当场死亡
        // （后者会让压测静默少掉并发，比 100% 错误率更难排查）。
        assertEquals(badStats.getTotalRequests(), badStats.getErrorCount(),
                "URL 含未命中占位符时请求构造必然失败：应逐次计为错误，且 worker 存活继续压测");
    }

    /**
     * 【评审 P1 回归】warmup 期产生的未命中<b>不得</b>泄漏进正式期统计。
     *
     * <p><b>为何要单独一个用例</b>：上一个用例 {@code warmupSec=0}，全程计入统计，
     * 无法区分「warmup 未命中被丢弃」与「从未产生未命中」。本用例刻意让某步骤
     * <b>必然</b>未命中（引用越界的 p9），并开启 warmup：
     * <ul>
     *   <li>正常（每迭代清零）：正式期每次迭代恰好 1 次未命中 → renderMiss == totalRequests；</li>
     *   <li>若计数跨迭代累加：warmup 期的未命中会被正式期第一次统计一并提交
     *       → renderMiss 远大于 totalRequests。</li>
     * </ul>
     * 判定用「等于 totalRequests」而非「等于 0」，因此与迭代数无关，是确定性断言。
     *
     * <p><b>未命中放在请求头而非 URL</b>：URL 里的未解析占位符含 {@code {}}，
     * 会构造出非法 URI 走另一条降级分支（该分支自身也会清零），从而掩盖本用例要测的泄漏。
     */
    @Test
    void warmupMissesDoNotLeakIntoFormalStats() {
        StressTaskRequest request = new StressTaskRequest();
        request.setConcurrency(2);
        request.setDurationSec(2);
        request.setWarmupSec(2);
        request.setSampleIntervalSec(1);

        StressTaskRequest.ScenarioStep home = new StressTaskRequest.ScenarioStep();
        home.setName("home");
        home.setUrl(base + "/home");
        home.setWeight(1);

        // 引用越界的 p9 且放在请求头里：每次渲染必然未命中一次，但不影响请求发出
        StressTaskRequest.ScenarioStep alwaysMiss = new StressTaskRequest.ScenarioStep();
        alwaysMiss.setName("alwaysMiss");
        alwaysMiss.setUrl(base + "/home");
        alwaysMiss.setWeight(1);
        alwaysMiss.setHeaders(Map.of("X-Token", "{{p9.result.data.token}}"));

        request.setScenario(List.of(home, alwaysMiss));

        StressTaskResult result = new HttpStressEngine().run(request, null);

        StressStatistician.StepStats stats = byName(result.getStepStats(), "alwaysMiss");
        assertTrue(stats.getTotalRequests() > 0, "未命中不得中断压测");
        assertEquals(stats.getTotalRequests(), stats.getRenderMiss(),
                "每次迭代恰好 1 次未命中；若 warmup 期未命中泄漏进正式期，该值会大于总请求数"
                        + "（total=" + stats.getTotalRequests() + ", miss=" + stats.getRenderMiss() + "）");
    }

    /**
     * 【分档模式】配置告警跨档去重 + 分步骤统计跨档合并。
     *
     * <p>{@code Scenario.of()} 在 {@code runStage} 内<b>按档重建</b>，同一条配置告警会被
     * 逐档重复产出，最终由 {@code run()} 的 LinkedHashSet 去重；分步骤统计则由
     * {@code mergeStepStats} 按步骤名累加。本用例锁定该链路。
     *
     * <p><b>未命中放在请求头而非 URL</b>：URL 里的未解析占位符会构造出非法 URI 走降级分支，
     * 该分支额外计一次错误，会干扰「分步之和 == 总请求数」的口径断言。
     */
    @Test
    void stagedScenarioDeduplicatesWarningsAndMergesStepStats() {
        StressTaskRequest request = new StressTaskRequest();
        request.setConcurrency(1);
        request.setWarmupSec(0);
        request.setSampleIntervalSec(1);

        StressTaskRequest.ScenarioStep home = new StressTaskRequest.ScenarioStep();
        home.setName("home");
        home.setUrl(base + "/home");
        home.setWeight(1);

        // 引用越界的 p9：启动期告警，运行期逐次计 renderMiss
        StressTaskRequest.ScenarioStep bad = new StressTaskRequest.ScenarioStep();
        bad.setName("bad");
        bad.setUrl(base + "/home");
        bad.setWeight(1);
        bad.setHeaders(Map.of("X-Token", "{{p9.result.a}}"));

        request.setScenario(List.of(home, bad));

        StressTaskRequest.Stage stage1 = new StressTaskRequest.Stage();
        stage1.setConcurrency(1);
        stage1.setDurationSec(2);
        StressTaskRequest.Stage stage2 = new StressTaskRequest.Stage();
        stage2.setConcurrency(1);
        stage2.setDurationSec(2);
        request.setStages(List.of(stage1, stage2));

        StressTaskResult result = new HttpStressEngine().run(request, null);

        // 去重：两档各自产出同一条告警，最终只应保留一条
        assertEquals(1, result.getWarnings().size(),
                "分档模式每档重建 Scenario，同一条告警应跨档去重，实际 " + result.getWarnings());
        assertTrue(result.getWarnings().get(0).contains("p9"),
                "告警内容应指出越界的步骤序号，实际 " + result.getWarnings());

        // 合并：分步骤统计按步骤名跨档累加（两档都有请求）
        StressStatistician.StepStats badStats = byName(result.getStepStats(), "bad");
        assertTrue(badStats.getTotalRequests() >= 2,
                "两档的请求数应按步骤名合并，实际 " + badStats.getTotalRequests());
        // renderMiss 是纯计数，跨档直接相加；本步骤每次迭代恰好 1 次未命中
        assertEquals(badStats.getTotalRequests(), badStats.getRenderMiss(),
                "renderMiss 为纯计数，跨档应直接相加");
        // 汇总口径：各步骤请求数之和 == 总请求数
        long stepSum = result.getStepStats().stream()
                .mapToLong(StressStatistician.StepStats::getTotalRequests).sum();
        assertEquals(stepSum, result.getTotalRequests(),
                "分档模式下分步统计之和应与总请求数一致");
    }

    @Test
    void singleInterfaceModeHasNoStepStats() {
        StressTaskRequest request = StressTaskRequest.single(base + "/home", 2, 3);
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
        // 时长取 3s：需保证两步都至少各发出一次请求（否则某步无统计条目，
        // 排序断言会因缺项而失败）；慢机器上 2s 窗口有此风险
        request.setDurationSec(3);
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
