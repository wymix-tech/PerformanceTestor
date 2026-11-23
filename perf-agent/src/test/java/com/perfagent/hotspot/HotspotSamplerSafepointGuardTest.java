package com.perfagent.hotspot;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.HttpReporter;
import com.perfagent.reporter.MetricsReporter;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * P1-5 热点采样 Safepoint 防呆：配置低于 20ms 下限被强制上调（观测者效应防护），
 * 默认值 50ms 与 agentArgs 解析保持兼容。
 */
class HotspotSamplerSafepointGuardTest {

    private static HotspotSampler sampler(int intervalMs) {
        AgentConfig cfg = new AgentConfig();
        cfg.hotspotEnabled = true;
        cfg.hotspotIntervalMs = intervalMs;
        return new HotspotSampler(cfg, new MetricsReporter(new AgentConfig(), new HttpReporter()));
    }

    @Test
    void intervalBelowFloorIsRaisedToMinimum() {
        assertEquals(20, sampler(10).effectiveIntervalMs);   // 旧默认 10ms → 上调
        assertEquals(20, sampler(1).effectiveIntervalMs);    // 极端值 → 上调
        assertEquals(20, sampler(0).effectiveIntervalMs);
    }

    @Test
    void intervalAtOrAboveFloorIsKept() {
        assertEquals(20, sampler(20).effectiveIntervalMs);
        assertEquals(50, sampler(50).effectiveIntervalMs);
        assertEquals(100, sampler(100).effectiveIntervalMs);
    }

    @Test
    void defaultConfigUsesSafeInterval() {
        assertEquals(50, new AgentConfig().hotspotIntervalMs, "新默认值 50ms");
    }

    @Test
    void agentArgsParsingStillHonored() {
        AgentConfig cfg = AgentConfig.fromArgs("appName=demo,hotspotIntervalMs=25,asyncTraceEnabled=true");
        assertEquals(25, cfg.hotspotIntervalMs);
        assertEquals(25, sampler(25).effectiveIntervalMs);
        assertEquals(true, cfg.asyncTraceEnabled);
    }
}
