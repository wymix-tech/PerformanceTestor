package com.perfserver.stress;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 步骤响应上下文池单测：懒解析、非 JSON、截断、原子替换的并发安全、别名登记规则。
 */
class StepContextStoreTest {

    private static final String JSON = "{\"data\":{\"token\":\"tk-1\"}}";

    /** 【评审 P2】parseStepRef 是别名拦截与运行期解析的唯一实现，四边界必须确定 */
    @Test
    void parseStepRefBoundaries() {
        assertEquals(1, StepContextStore.parseStepRef("p1"));
        assertEquals(2, StepContextStore.parseStepRef("p2"));
        assertEquals(2, StepContextStore.parseStepRef("P2"), "前缀大小写不敏感");
        assertEquals(12, StepContextStore.parseStepRef("p12"));
        // 非法：序号从 1 开始 / 前导零 / 非数字后缀 / 空 / 溢出
        assertEquals(-1, StepContextStore.parseStepRef("p0"));
        assertEquals(-1, StepContextStore.parseStepRef("p01"));
        assertEquals(-1, StepContextStore.parseStepRef("p1x"));
        assertEquals(-1, StepContextStore.parseStepRef("p"));
        assertEquals(-1, StepContextStore.parseStepRef(""));
        assertEquals(-1, StepContextStore.parseStepRef(null));
        assertEquals(-1, StepContextStore.parseStepRef("p99999999999999999999"));
        assertEquals(-1, StepContextStore.parseStepRef("login"));
    }

    @Test
    void indexOfResolvesOrdinalAndAlias() {
        Map<String, Integer> alias = new HashMap<>();
        alias.put("login", 0);
        StepContextStore ctx = new StepContextStore(2, alias, 4096);
        assertEquals(0, ctx.indexOf("p1"));
        assertEquals(1, ctx.indexOf("p2"));
        assertEquals(0, ctx.indexOf("login"));
        // 越界序号 / 未登记名字 / 非法序号 → 都不是上下文引用
        assertEquals(-1, ctx.indexOf("p3"));
        assertEquals(-1, ctx.indexOf("p0"));
        assertEquals(-1, ctx.indexOf("nope"));
        assertEquals(-1, ctx.indexOf(null));
    }

    @Test
    void resolvesWholeBodyAndPaths() {
        StepContextStore ctx = new StepContextStore(1, new HashMap<>(), 4096);
        // 未写入时一切未命中
        assertNull(ctx.resolve(0, "result", null));
        assertNull(ctx.resolve(0, "header", "X-Token"));
        ctx.put(0, Map.of("X-Token", List.of("tk")), JSON);
        assertEquals(JSON, ctx.resolve(0, "result", null));
        assertEquals(JSON, ctx.resolve(0, "result", ""));
        assertEquals("tk-1", ctx.resolve(0, "result", "data.token"));
        assertEquals("tk", ctx.resolve(0, "header", "x-token"));
        assertNull(ctx.resolve(0, "result", "data.nope"));
    }

    /** 非 JSON 响应：{{pN.result}} 返回原文，带 path 时无法下钻（且不反复解析失败） */
    @Test
    void nonJsonBodyKeepsRawAndNeverParses() {
        StepContextStore ctx = new StepContextStore(1, new HashMap<>(), 4096);
        ctx.put(0, null, "<html>502 Bad Gateway</html>");
        assertEquals("<html>502 Bad Gateway</html>", ctx.resolve(0, "result", null));
        assertNull(ctx.resolve(0, "result", "data.token"));
        // 重复取值结果稳定（jsonParsed 已置位，不会重复解析）
        assertNull(ctx.resolve(0, "result", "data.token"));
    }

    /** 基本类型响应体（如 "12345"、true）按原文返回 */
    @Test
    void scalarBodyReturnsRawText() {
        StepContextStore ctx = new StepContextStore(1, new HashMap<>(), 4096);
        ctx.put(0, null, "12345");
        assertEquals("12345", ctx.resolve(0, "result", null));
    }

    @Test
    void truncatesOversizedBody() {
        int max = 64;
        StepContextStore ctx = new StepContextStore(1, new HashMap<>(), max);
        String big = "x".repeat(500);
        ctx.put(0, null, big);
        assertEquals(max, ctx.resolve(0, "result", null).length());
    }

    /** 错误响应（status>=400 / 断言未过）照常入池：下游引用"最新可达数据"（设计 2.3.5） */
    @Test
    void errorResponseStillEntersStore() {
        StepContextStore ctx = new StepContextStore(1, new HashMap<>(), 4096);
        ctx.put(0, null, "{\"data\":{\"token\":\"good\"}}");
        // 模拟后续一次失败响应覆盖槽位
        ctx.put(0, null, "{\"error\":\"upstream down\"}");
        assertEquals("upstream down", ctx.resolve(0, "result", "error"));
        assertNull(ctx.resolve(0, "result", "data.token"), "旧快照已被整体替换");
    }

    /**
     * 并发压测下的健壮性：多线程交替写槽位 + 并发读（含并发首次懒解析），
     * 不得抛异常、不得读到残缺/损坏的值。
     *
     * <p><b>为何不直接断言「header 与 body 同版本」</b>：两次独立的 {@code resolve}
     * 调用之间本就可能跨过一次 {@code put}，此时读到不同版本属正常 interleaving，
     * 与快照撕裂无法区分。「同版本」由槽位快照的不可变性 +
     * {@code AtomicReferenceArray} 整体替换<b>结构性</b>保证（读侧拿到的永远是
     * 某一个完整的 {@code StepResponse}），故这里只断言不会读到损坏数据。
     */
    @Test
    void concurrentPutAndResolveNeverCorrupt() throws Exception {
        StepContextStore ctx = new StepContextStore(1, new HashMap<>(), 4096);
        int writers = 4;
        int readers = 4;
        int rounds = 3000;
        CountDownLatch start = new CountDownLatch(1);
        AtomicReference<String> failure = new AtomicReference<>();
        List<Thread> threads = new ArrayList<>();

        for (int w = 0; w < writers; w++) {
            final int id = w;
            Thread t = new Thread(() -> {
                await(start);
                try {
                    for (int i = 0; i < rounds; i++) {
                        String tag = id + "-" + i;
                        ctx.put(0, Map.of("X-Tag", List.of(tag)), "{\"tag\":\"" + tag + "\"}");
                    }
                } catch (Throwable e) {
                    failure.compareAndSet(null, "writer failed: " + e);
                }
            }, "writer-" + w);
            threads.add(t);
        }
        for (int r = 0; r < readers; r++) {
            Thread t = new Thread(() -> {
                await(start);
                try {
                    for (int i = 0; i < rounds; i++) {
                        String bodyTag = ctx.resolve(0, "result", "tag");
                        String headerTag = ctx.resolve(0, "header", "X-Tag");
                        // 任一时刻读到的都必须是某个完整版本的 tag（形如 "<id>-<i>"），
                        // 不允许出现截断 JSON、空串或半截值
                        assertWellFormedTag(bodyTag, failure);
                        assertWellFormedTag(headerTag, failure);
                    }
                } catch (Throwable e) {
                    failure.compareAndSet(null, "reader failed: " + e);
                }
            }, "reader-" + r);
            threads.add(t);
        }
        threads.forEach(Thread::start);
        start.countDown();
        for (Thread t : threads) {
            t.join(30_000);
        }
        assertNull(failure.get(), "并发读写不得抛异常或读到损坏数据");
    }

    private static void assertWellFormedTag(String tag, AtomicReference<String> failure) {
        if (tag == null) {
            return; // 尚未写入
        }
        int dash = tag.indexOf('-');
        if (dash <= 0 || dash == tag.length() - 1) {
            failure.compareAndSet(null, "malformed tag: " + tag);
        }
    }

    /** 懒解析：并发首次引用同一槽位只解析一次结果且一致 */
    @Test
    void lazyJsonParsingIsThreadSafe() throws Exception {
        StepContextStore ctx = new StepContextStore(1, new HashMap<>(), 4096);
        ctx.put(0, null, "{\"data\":{\"token\":\"tk-concurrent\"}}");
        int n = 8;
        CountDownLatch start = new CountDownLatch(1);
        List<Thread> threads = new ArrayList<>();
        List<String> results = java.util.Collections.synchronizedList(new ArrayList<>());
        for (int i = 0; i < n; i++) {
            Thread t = new Thread(() -> {
                await(start);
                results.add(ctx.resolve(0, "result", "data.token"));
            });
            threads.add(t);
        }
        threads.forEach(Thread::start);
        start.countDown();
        for (Thread t : threads) {
            t.join(10_000);
        }
        assertEquals(n, results.size());
        results.forEach(v -> assertEquals("tk-concurrent", v));
    }

    @Test
    void buildAliasIndexRegistersNameFallbackAndDedupes() {
        List<StressTaskRequest.ScenarioStep> steps = new ArrayList<>();
        steps.add(step("login", null));        // 回退用 name
        steps.add(step("order", "orderAlias"));// 显式 alias 优先
        steps.add(step("detail", "orderAlias"));// 重名：首个生效 + 告警
        steps.add(step(null, "checkout"));     // 无 name，仅 alias
        List<String> warnings = new ArrayList<>();
        Map<String, Integer> index = StepContextStore.buildAliasIndex(steps, warnings);

        assertEquals(0, index.get("login").intValue());
        assertEquals(1, index.get("orderAlias").intValue());
        assertEquals(3, index.get("checkout").intValue());
        assertFalse(index.containsKey("order"), "显式 alias 存在时 name 不再兼作别名");
        assertTrue(warnings.stream().anyMatch(w -> w.contains("重名")), "重名应产生告警");
    }

    /** pN 形式的别名被拦截（序号引用优先级最高），非法别名被忽略 */
    @Test
    void buildAliasIndexRejectsStepRefShapedAndInvalidAliases() {
        List<StressTaskRequest.ScenarioStep> steps = new ArrayList<>();
        steps.add(step("a", "p2"));
        steps.add(step("b", "bad alias!"));
        steps.add(step("中文名", null));  // 回退的 name 非法：只 DEBUG，不告警
        List<String> warnings = new ArrayList<>();
        Map<String, Integer> index = StepContextStore.buildAliasIndex(steps, warnings);

        assertTrue(index.isEmpty(), "pN 形式与非法别名都不得登记");
        assertEquals(2, warnings.size(), "两条显式声明的问题都应告警；回退 name 非法不告警");
        assertTrue(warnings.stream().anyMatch(w -> w.contains("p2")));
        assertTrue(warnings.stream().anyMatch(w -> w.contains("不合法")));
    }

    @Test
    void isValidAliasAcceptsIdentifiersOnly() {
        assertTrue(StepContextStore.isValidAlias("login"));
        assertTrue(StepContextStore.isValidAlias("_a1"));
        assertTrue(StepContextStore.isValidAlias("a-b"));
        assertFalse(StepContextStore.isValidAlias("1a"));
        assertFalse(StepContextStore.isValidAlias("a b"));
        assertFalse(StepContextStore.isValidAlias("中文"));
        assertFalse(StepContextStore.isValidAlias(null));
    }

    private static StressTaskRequest.ScenarioStep step(String name, String alias) {
        StressTaskRequest.ScenarioStep s = new StressTaskRequest.ScenarioStep();
        s.setName(name);
        s.setAlias(alias);
        return s;
    }

    private static void await(CountDownLatch latch) {
        try {
            latch.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
