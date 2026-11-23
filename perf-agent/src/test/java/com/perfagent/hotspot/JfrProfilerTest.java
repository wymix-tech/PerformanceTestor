package com.perfagent.hotspot;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.HttpReporter;
import com.perfagent.reporter.MetricsReporter;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicLong;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * JfrProfiler 端到端行为测试（P2-1）：在测试 JVM 内实际开启 JFR RecordingStream，
 * 人工构造 CPU 热点 / 对象分配 / synchronized 锁竞争三类负载，验证能捕获并输出
 * 合法折叠栈；stop() 幂等且不再产生新数据（无残留采集线程）。
 */
class JfrProfilerTest {

    /** 记录型上报器：截获 profile / hotspot 调用，不发 HTTP */
    static final class RecordingReporter extends MetricsReporter {
        final List<Map<String, Object>> profiles = new CopyOnWriteArrayList<>();
        final List<List<Map<String, Object>>> hotspots = new CopyOnWriteArrayList<>();

        RecordingReporter(AgentConfig cfg) {
            super(cfg, new HttpReporter());
        }

        @Override
        public void reportProfile(String profileType, String foldedStacks,
                                  long totalSamples, long sampleTimeMs) {
            Map<String, Object> m = new HashMap<>();
            m.put("profileType", profileType);
            m.put("foldedStacks", foldedStacks);
            m.put("totalSamples", totalSamples);
            m.put("sampleTime", sampleTimeMs);
            profiles.add(m);
        }

        @Override
        public void reportHotspot(List<Map<String, Object>> topMethods) {
            hotspots.add(topMethods);
        }
    }

    private static final AtomicLong SINK = new AtomicLong();
    private static final Object MONITOR = new Object();

    /** CPU 密集负载（ExecutionSample 应命中） */
    static void cpuBurn() {
        long acc = 0;
        for (int i = 0; i < 300_000_000; i++) {
            acc += i % 7;
        }
        SINK.addAndGet(acc);
    }

    /** 对象分配负载（ObjectAllocationSample 应命中） */
    static void allocBurn() {
        long bytes = 0;
        for (int i = 0; i < 400_000; i++) {
            byte[] b = new byte[2048];
            bytes += b.length;
        }
        SINK.addAndGet(bytes);
    }

    /** synchronized 锁竞争负载（JavaMonitorEnter 应命中） */
    static void lockBurn() {
        for (int i = 0; i < 200; i++) {
            synchronized (MONITOR) {
                try {
                    Thread.sleep(5);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
        }
    }

    private static AgentConfig jfrConfig() {
        AgentConfig cfg = new AgentConfig();
        cfg.hotspotEnabled = true;
        cfg.profilerMode = "jfr";
        cfg.jfrCpuIntervalMs = 10;
        cfg.jfrAllocThrottle = 1000;
        cfg.jfrLockThresholdMs = 1;
        cfg.hotspotReportSec = 1;
        return cfg;
    }

    private static void runThreads(Runnable body, int parallel, long millis) {
        for (int i = 0; i < parallel; i++) {
            Thread t = new Thread(() -> {
                long deadline = System.currentTimeMillis() + millis;
                while (System.currentTimeMillis() < deadline) {
                    body.run();
                }
            }, "perf-test-load-" + i);
            t.setDaemon(true);
            t.start();
        }
    }

    /** 折叠栈行合法性：'帧1;帧2 count'，帧为 pkg.Class.method，探针/JFR 内部帧被过滤 */
    private static void assertFoldedLineValid(String line) {
        String[] parts = line.trim().split(" ");
        assertEquals(2, parts.length, "折叠栈行格式应为 '栈 count': " + line);
        assertTrue(Long.parseLong(parts[1]) > 0);
        for (String frame : parts[0].split(";")) {
            assertFalse(frame.isBlank(), "帧不应为空: " + line);
            assertTrue(frame.contains("."), "帧应为 pkg.Class.method 形态: " + frame);
            assertFalse(frame.startsWith("com.perfagent.")
                            || frame.startsWith("net.bytebuddy.")
                            || frame.startsWith("jdk.jfr."),
                    "探针/JFR 内部帧应被过滤: " + frame);
        }
    }

    @Test
    @Timeout(90)
    void capturesCpuAllocLockAndEmitsValidFoldedStacks() throws Exception {
        RecordingReporter reporter = new RecordingReporter(new AgentConfig());
        JfrProfiler profiler = new JfrProfiler(jfrConfig(), reporter);
        profiler.start();
        assertTrue(profiler.isRunning());

        // 三类负载并行跑 4s（上报周期 1s，期间至少产生 2 次周期快照 + 尾窗兜底）
        runThreads(JfrProfilerTest::cpuBurn, 2, 4000);
        runThreads(JfrProfilerTest::allocBurn, 1, 4000);
        runThreads(JfrProfilerTest::lockBurn, 3, 4000);
        Thread.sleep(4200);

        profiler.stop();
        assertFalse(profiler.isRunning());
        int countAfterStop = reporter.profiles.size();

        // stop 幂等：再次调用无副作用，不产生新数据
        profiler.stop();
        assertEquals(countAfterStop, reporter.profiles.size());

        // 三类 profileType 均被实际捕获，且折叠栈行全部合法
        for (String expected : new String[]{"cpu", "alloc", "lock"}) {
            boolean captured = reporter.profiles.stream()
                    .anyMatch(p -> expected.equals(p.get("profileType")));
            assertTrue(captured, "应捕获 profileType=" + expected
                    + "，实际: " + reporter.profiles.stream()
                    .map(p -> p.get("profileType")).toList());
        }
        for (Map<String, Object> p : reporter.profiles) {
            assertTrue((Long) p.get("totalSamples") > 0, "totalSamples 应为正");
            String folded = (String) p.get("foldedStacks");
            assertFalse(folded.isBlank(), "折叠栈不应为空");
            for (String line : folded.split("\n")) {
                assertFoldedLineValid(line);
            }
        }

        // 双轨兼容：cpu 折叠栈叶帧应同时产出 hotspot 排行（既有排行零破坏）
        boolean hotspotCompat = reporter.hotspots.stream()
                .flatMap(List::stream)
                .anyMatch(m -> m.containsKey("method") && m.containsKey("samples")
                        && ((Number) m.get("samples")).longValue() > 0);
        assertTrue(hotspotCompat, "cpu 栈叶帧应兼容产出 reportHotspot 排行");
    }

    @Test
    @Timeout(30)
    void stopBeforeStartIsSafe() {
        JfrProfiler profiler = new JfrProfiler(jfrConfig(),
                new RecordingReporter(new AgentConfig()));
        profiler.stop(); // 未启动直接 stop：幂等无异常
        assertFalse(profiler.isRunning());
    }
}
