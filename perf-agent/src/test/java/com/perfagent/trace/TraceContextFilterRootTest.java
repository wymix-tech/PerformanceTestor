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
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * TraceContext Filter 根模式状态机单元测试。
 *
 * <p>验证修复目标：采样决策只在 Filter 入口发生一次；
 * 未采样请求整链短路（下游方法不再重复成为根，消除采样稀释）。
 */
class TraceContextFilterRootTest {

    private static final double ALWAYS = 1.0;
    private static final double NEVER = 0.0;

    /** 记录型替身：捕获提交的调用链根节点与染色标识（真实 reporter 未 start 时为 no-op） */
    private static final class RecordingReporter extends MetricsReporter {
        final List<TraceNode> submittedRoots = new CopyOnWriteArrayList<>();
        final List<String> submittedStressTaskIds = new CopyOnWriteArrayList<>();

        RecordingReporter() {
            super(new AgentConfig(), new HttpReporter());
        }

        @Override
        public void reportTrace(String traceId, String app, TraceNode root, int nodeCount, long startTimeMs) {
            reportTrace(traceId, app, root, nodeCount, startTimeMs, null);
        }

        @Override
        public void reportTrace(String traceId, String app, TraceNode root, int nodeCount,
                                long startTimeMs, String stressTaskId) {
            submittedRoots.add(root);
            submittedStressTaskIds.add(stressTaskId);
        }
    }

    private RecordingReporter reporter;

    @BeforeEach
    void setUp() {
        reporter = new RecordingReporter();
        TraceContext.init("test-app", new TraceSampler(ALWAYS, 0), reporter);
    }

    @AfterEach
    void tearDown() {
        TraceContext.cleanup();
    }

    private void initSampler(double rate) {
        TraceContext.init("test-app", new TraceSampler(rate, 0), reporter);
    }

    @Test
    void sampledRequestProducesCompleteChainFromFirstBusinessMethod() {
        // Filter 进入（采样命中）：栈空但已采样
        assertTrue(TraceContext.beginFilterRoot());
        assertTrue(TraceContext.inSampledContext());

        // 首个业务方法（Controller）成为根节点
        TraceContext.begin("com.demo.controller.UserController#list");
        // 中间层（Service）正常入栈
        TraceContext.begin("com.demo.service.UserService#listUsers");
        TraceContext.end(System.nanoTime() - 1_000_000);

        // 根退出 → 提交整条链路（根 = Controller，非 Service）
        TraceContext.end(System.nanoTime() - 2_000_000);
        assertEquals(1, reporter.submittedRoots.size());
        assertEquals("com.demo.controller.UserController#list", reporter.submittedRoots.get(0).method);
        assertEquals(1, reporter.submittedRoots.get(0).children.size());
        TraceContext.endFilterRoot(); // Filter 退出清理
        assertTrue(TraceContext.isRoot());
    }

    @Test
    void notSampledRequestShortCircuitsWholeChain() {
        initSampler(NEVER);
        // Filter 进入（未采样）→ 创建 NOT_SAMPLED 占位
        assertTrue(TraceContext.beginFilterRoot());
        // 整链短路：下游方法判断一次即跳过，不再触发采样（旧实现此处会重复 beginRoot）
        assertFalse(TraceContext.inSampledContext());
        TraceContext.begin("com.demo.service.UserService#listUsers");
        TraceContext.end(System.nanoTime());
        // Filter 退出清理
        TraceContext.endFilterRoot();
        assertTrue(TraceContext.isRoot());
        assertEquals(0, reporter.submittedRoots.size());
    }

    @Test
    void nestedFilterIsNotOwnerAndDoesNotDisturbChain() {
        // 最外层 Filter：owner
        assertTrue(TraceContext.beginFilterRoot());
        // 嵌套 Filter：非 owner（advice enter 返回 false → exit 不做任何处理，此处模拟该行为）
        assertFalse(TraceContext.beginFilterRoot());
        // 业务链路正常
        TraceContext.begin("com.demo.controller.OrderController#create");
        TraceContext.end(System.nanoTime());
        // 嵌套 Filter 退出：非 owner 的 advice 不调用 endFilterRoot，上下文仍在
        assertFalse(TraceContext.isRoot());
        // 最外层 Filter 退出才清理
        TraceContext.endFilterRoot();
        assertTrue(TraceContext.isRoot());
    }

    @Test
    void contextSurvivesAfterBusinessRootExitUntilFilterExits() {
        // 回归测试：业务根方法退出提交链路后，上下文必须保留至 Filter 退出，
        // 否则后续方法（拦截器后置处理等）会重新成为根（采样稀释复现）
        assertTrue(TraceContext.beginFilterRoot());
        TraceContext.begin("com.demo.controller.UserController#list");
        TraceContext.end(System.nanoTime());
        assertEquals(1, reporter.submittedRoots.size());
        assertFalse(TraceContext.isRoot());
        assertTrue(TraceContext.inSampledContext());
        // 后续方法仍挂在"已采样"标志下（不会开启新链路）
        TraceContext.begin("com.demo.interceptor.PostInterceptor#afterCompletion");
        TraceContext.end(System.nanoTime());
        assertEquals(1, reporter.submittedRoots.size()); // 未提交第二条
        // Filter 退出统一清理
        TraceContext.endFilterRoot();
        assertTrue(TraceContext.isRoot());
    }

    @Test
    void secondDispatchWithinSameFilterIsSuppressed() {
        // 一请求至多一条链路：主链路提交后，同请求内后续业务方法（二次分发/后置处理）不再开启新链路
        assertTrue(TraceContext.beginFilterRoot());
        TraceContext.begin("com.demo.controller.UserController#list");
        TraceContext.end(System.nanoTime());
        TraceContext.begin("com.demo.controller.OrderController#list");
        TraceContext.end(System.nanoTime());
        assertEquals(1, reporter.submittedRoots.size());
        assertEquals("com.demo.controller.UserController#list", reporter.submittedRoots.get(0).method);
        TraceContext.endFilterRoot();
        assertTrue(TraceContext.isRoot());
    }

    @Test
    void emptyChainRequestIsCleanedUpWithoutReport() {
        // 静态资源等：Filter 进入采样命中，但请求未触达业务方法
        assertTrue(TraceContext.beginFilterRoot());
        TraceContext.endFilterRoot();
        assertTrue(TraceContext.isRoot());
        assertEquals(0, reporter.submittedRoots.size());
    }

    @Test
    void methodChainSnapshotWorksInFilterRootMode() {
        assertTrue(TraceContext.beginFilterRoot());
        TraceContext.begin("com.demo.controller.UserController#list");
        TraceContext.begin("com.demo.service.UserService#listUsers");
        List<String> chain = TraceContext.currentMethodChain();
        assertEquals(List.of("com.demo.controller.UserController#list",
                "com.demo.service.UserService#listUsers"), chain);
    }

    @Test
    void legacyBeginRootStillWorksForNonHttpScenario() {
        // 非 HTTP 场景（Filter 未拦截到，如 MQ 消费者/定时任务）：业务方法自行决策采样（旧行为兼容）
        String traceId = TraceContext.beginRoot("com.demo.task.ScheduledJob#run");
        assertNotNull(traceId);
        TraceContext.end(System.nanoTime());
        assertEquals(1, reporter.submittedRoots.size());
        assertEquals("com.demo.task.ScheduledJob#run", reporter.submittedRoots.get(0).method);
    }

    // ===================== P1-1 压测流量染色 =====================

    /** 测试用最小请求替身：与 Servlet API 无关，仅提供 getHeader(String) 方法签名 */
    static class FakeServletRequest {
        String value;

        public String getHeader(String name) {
            return value;
        }
    }

    /** 不带 getHeader 的替身（非 Servlet 请求）：染色提取应安全短路 */
    static class PlainObject {
    }

    @Test
    void stressTaskIdCapturedEvenWhenNotSampled() {
        // 关键验收：sampleRate=0.0 时染色标识仍必须进上下文
        //（慢 SQL / 慢命令独立于调用链采样率触发，未采样请求同样要携带 stressTaskId）
        initSampler(NEVER);
        FakeServletRequest req = new FakeServletRequest();
        req.value = "42";
        assertTrue(TraceContext.beginFilterRoot(req));
        assertFalse(TraceContext.inSampledContext());
        assertEquals("42", TraceContext.currentStressTaskId());
        TraceContext.endFilterRoot();
    }

    @Test
    void stressTaskIdPropagatesToSubmittedTrace() {
        FakeServletRequest req = new FakeServletRequest();
        req.value = "1001";
        assertTrue(TraceContext.beginFilterRoot(req));
        assertEquals("1001", TraceContext.currentStressTaskId());
        TraceContext.begin("com.demo.controller.OrderController#create");
        TraceContext.end(System.nanoTime());
        assertEquals(1, reporter.submittedRoots.size());
        assertEquals("1001", reporter.submittedStressTaskIds.get(0));
        TraceContext.endFilterRoot();
    }

    @Test
    void missingOrInvalidHeaderYieldsNullStressTaskId() {
        // 未染色流量（无头 / 非 Servlet 请求 / null 请求）：不抛异常，标识为 null
        assertTrue(TraceContext.beginFilterRoot(new FakeServletRequest()));
        assertNull(TraceContext.currentStressTaskId());
        TraceContext.endFilterRoot();

        assertTrue(TraceContext.beginFilterRoot(new PlainObject()));
        assertNull(TraceContext.currentStressTaskId());
        TraceContext.endFilterRoot();

        assertTrue(TraceContext.beginFilterRoot(null));
        assertNull(TraceContext.currentStressTaskId());
        TraceContext.endFilterRoot();
        // 无参旧重载（兼容既有调用方）仍可用
        assertTrue(TraceContext.beginFilterRoot());
        assertNull(TraceContext.currentStressTaskId());
        TraceContext.endFilterRoot();
    }
}
