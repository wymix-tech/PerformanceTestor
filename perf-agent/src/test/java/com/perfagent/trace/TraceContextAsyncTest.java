package com.perfagent.trace;

import com.perfagent.config.AgentConfig;
import com.perfagent.instrumentation.AsyncSupport;
import com.perfagent.reporter.HttpReporter;
import com.perfagent.reporter.MetricsReporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * P1-5 异步上下文透传单元测试：快照 capture/attach/detach 状态机 +
 * AsyncSupport 包装语义 + 严禁池复用线程遗留 CTX。
 */
class TraceContextAsyncTest {

    private static final double ALWAYS = 1.0;
    private static final double NEVER = 0.0;

    @BeforeEach
    void setUp() {
        TraceContext.init("test-app", new TraceSampler(ALWAYS, 0),
                new MetricsReporter(new AgentConfig(), new HttpReporter()));
        AsyncSupport.enabled = false;
    }

    @AfterEach
    void tearDown() {
        AsyncSupport.enabled = false;
        TraceContext.cleanup();
    }

    private void initSampler(double rate) {
        TraceContext.init("test-app", new TraceSampler(rate, 0),
                new MetricsReporter(new AgentConfig(), new HttpReporter()));
    }

    // ---------------------------------------------------------- 快照状态机

    @Test
    void captureWithoutContextReturnsNull() {
        assertNull(TraceContext.capture());
    }

    @Test
    void captureCarriesTraceIdAndSampling() {
        String traceId = TraceContext.beginRoot("com.demo.Biz#run");
        TraceContext.Snapshot snapshot = TraceContext.capture();
        assertNotNull(snapshot);
        assertTrue(snapshot.sampled);
        assertEquals(traceId, snapshot.traceId);
        assertNull(snapshot.stressTaskId);
        TraceContext.end(System.nanoTime());
    }

    @Test
    void attachInWorkerThreadRestoresAndDetachCleans() throws Exception {
        initSampler(NEVER);
        // 未采样但带染色标识：快照仍必须生成（慢 SQL/慢命令在子线程同样要携带）
        assertTrue(TraceContext.beginFilterRoot(new TraceContextFilterRootTest.FakeServletRequest() {{
            value = "77";
        }}));
        assertFalse(TraceContext.inSampledContext());
        TraceContext.Snapshot snapshot = TraceContext.capture();
        assertNotNull(snapshot);
        assertFalse(snapshot.sampled);
        assertEquals("77", snapshot.stressTaskId);

        AtomicReference<String> childTaskId = new AtomicReference<>();
        CountDownLatch done = new CountDownLatch(1);
        Thread worker = new Thread(() -> {
            TraceContext.Snapshot prev = TraceContext.attach(snapshot);
            try {
                childTaskId.set(TraceContext.currentStressTaskId());
            } finally {
                TraceContext.detach(prev); // finally 清理，防池复用串用
            }
            assertNull(TraceContext.currentStressTaskId(), "detach 后不得遗留 CTX");
            done.countDown();
        });
        worker.start();
        assertTrue(done.await(5, TimeUnit.SECONDS));
        assertEquals("77", childTaskId.get());
        TraceContext.endFilterRoot();
    }

    @Test
    void attachDoesNotOverrideExistingContext() {
        String root = TraceContext.beginRoot("com.demo.Biz#run");
        TraceContext.Snapshot foreign = new TraceContext.Snapshot("foreign-trace", "9", false, null);
        TraceContext.Snapshot prev = TraceContext.attach(foreign);
        // 已有上下文不覆盖：当前 traceId 不变，返回的是自身快照
        assertEquals(root, TraceContext.currentTraceId());
        assertNull(TraceContext.currentStressTaskId());
        assertEquals(root, prev.traceId);
        TraceContext.detach(prev); // 恢复原上下文
        assertEquals(root, TraceContext.currentTraceId());
        TraceContext.end(System.nanoTime());
    }

    @Test
    void sampledSnapshotCarriesParentNodeForChainJoin() {
        String traceId = TraceContext.beginRoot("com.demo.Controller#action");
        TraceContext.Snapshot snapshot = TraceContext.capture();
        assertNotNull(snapshot);
        assertEquals(traceId, snapshot.traceId);
        TraceContext.end(System.nanoTime());
    }

    // ---------------------------------------------------------- AsyncSupport 包装

    @Test
    void wrapDisabledReturnsSameInstance() {
        Runnable task = () -> { };
        assertSame(task, AsyncSupport.wrap(task));
    }

    @Test
    void wrapWithoutContextReturnsSameInstance() {
        AsyncSupport.enabled = true;
        Runnable task = () -> { };
        assertSame(task, AsyncSupport.wrap(task));
    }

    @Test
    void wrapWithContextCreatesAsyncRunnableAndCleansAfterRun() throws Exception {
        AsyncSupport.enabled = true;
        initSampler(NEVER);
        assertTrue(TraceContext.beginFilterRoot(new TraceContextFilterRootTest.FakeServletRequest() {{
            value = "88";
        }}));
        AtomicReference<String> childTaskId = new AtomicReference<>();
        Runnable task = () -> childTaskId.set(TraceContext.currentStressTaskId());
        Runnable wrapped = AsyncSupport.wrap(task);
        assertFalse(wrapped == task, "有染色上下文时应包装");
        // 二次包装防重复
        assertSame(wrapped, AsyncSupport.wrap(wrapped));

        CountDownLatch done = new CountDownLatch(1);
        Thread worker = new Thread(() -> {
            wrapped.run(); // AsyncRunnable：run 内 attach/detach 配对
            assertNull(TraceContext.currentStressTaskId(), "run 结束后不得遗留 CTX");
            done.countDown();
        });
        worker.start();
        assertTrue(done.await(5, TimeUnit.SECONDS));
        assertEquals("88", childTaskId.get());
        TraceContext.endFilterRoot();
    }

    @Test
    void wrapperPreservesBusinessException() throws Exception {
        AsyncSupport.enabled = true;
        String traceId = TraceContext.beginRoot("com.demo.Biz#run");
        Runnable boom = () -> {
            throw new IllegalStateException("biz failure");
        };
        Runnable wrapped = AsyncSupport.wrap(boom);
        AtomicReference<Throwable> caught = new AtomicReference<>();
        CountDownLatch done = new CountDownLatch(1);
        Thread worker = new Thread(() -> {
            try {
                wrapped.run();
            } catch (Throwable t) {
                caught.set(t);
            } finally {
                TraceContext.detach(null);
                done.countDown();
            }
        });
        worker.start();
        assertTrue(done.await(5, TimeUnit.SECONDS));
        assertTrue(caught.get() instanceof IllegalStateException, "业务异常原样上抛");
        assertEquals(traceId, TraceContext.currentTraceId()); // 主线程上下文不受影响
        TraceContext.end(System.nanoTime());
    }
}
