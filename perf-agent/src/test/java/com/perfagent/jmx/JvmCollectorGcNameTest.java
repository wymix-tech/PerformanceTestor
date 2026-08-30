package com.perfagent.jmx;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.HttpReporter;
import com.perfagent.reporter.MetricsReporter;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * GC 收集器名称精确映射测试。
 *
 * <p>回归旧实现缺陷：子串匹配（contains）导致 "PS MarkSweep" 含 "ps" 被误判为 Young、
 * Serial 的 "Copy" 不含任何关键字被误判为 Full、ZGC/Shenandoah 全归 Full。
 */
class JvmCollectorGcNameTest {

    private final JvmCollector collector =
            new JvmCollector(new AgentConfig(), new MetricsReporter(new AgentConfig(), new HttpReporter()));

    @Test
    void youngCollectorNamesAreClassifiedAsYoung() {
        assertTrue(collector.isYoungGc("Copy"), "Serial Young（旧实现误判为 Full）");
        assertTrue(collector.isYoungGc("PS Scavenge"));
        assertTrue(collector.isYoungGc("ParNew"));
        assertTrue(collector.isYoungGc("G1 Young Generation"));
        assertTrue(collector.isYoungGc("ZGC Cycles"));
        assertTrue(collector.isYoungGc("Shenandoah Cycles"));
    }

    @Test
    void oldCollectorNamesAreNotMisclassifiedAsYoung() {
        // 旧实现核心缺陷回归："PS MarkSweep".contains("ps") == true 曾被误判为 Young
        assertFalse(collector.isYoungGc("PS MarkSweep"), "Parallel Old 曾被子串 'ps' 误判为 Young");
        assertFalse(collector.isYoungGc("MarkSweepCompact"));
        assertFalse(collector.isYoungGc("ConcurrentMarkSweep"));
        assertFalse(collector.isYoungGc("G1 Old Generation"));
        assertFalse(collector.isYoungGc("G1 Mixed Generation"));
        assertFalse(collector.isYoungGc("G1 Concurrent GC"));
        assertFalse(collector.isYoungGc("ZGC Pauses"));
        assertFalse(collector.isYoungGc("Shenandoah Pauses"));
        assertFalse(collector.isYoungGc("ZGC"));
    }

    @Test
    void unknownCollectorNameFallsToOld() {
        // 未来新收集器：保守归 Full（运行时有一次性告警）
        assertFalse(collector.isYoungGc("SomeFutureGC"));
    }
}
