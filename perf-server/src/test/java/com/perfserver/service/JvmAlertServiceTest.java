package com.perfserver.service;

import com.perfserver.entity.JvmAlertRule;
import com.perfserver.entity.JvmMetric;
import com.perfserver.mapper.JvmAlertRuleMapper;
import com.perfserver.mapper.JvmMetricMapper;
import com.perfserver.notify.NotifyService;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.contains;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.ArgumentMatchers.nullable;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * JvmAlertService 单元测试：指标聚合、冷却期抑制、告警触发与规则校验。
 */
class JvmAlertServiceTest {

    // ---------------- 指标聚合 ----------------

    @Test
    void aggregatesHeapUsedRatioAsMax() {
        List<JvmMetric> samples = List.of(
                metric(1024.0, 2048.0, 0, 0),  // 50%
                metric(1800.0, 2048.0, 0, 0),  // 87.89%
                metric(512.0, 2048.0, 0, 0));  // 25%
        double ratio = JvmAlertService.aggregate(JvmAlertRule.HEAP_USED_RATIO, samples);
        assertEquals(87.89, ratio, 0.01);
    }

    @Test
    void heapRatioSkipsInvalidHeapMax() {
        List<JvmMetric> samples = List.of(
                metric(1024.0, 0.0, 0, 0),      // heapMax=0 无效
                metric(1024, null, 0, 0));  // heapMax 缺失
        assertTrue(Double.isNaN(JvmAlertService.aggregate(JvmAlertRule.HEAP_USED_RATIO, samples)));
    }

    @Test
    void aggregatesThreadCountAsMax() {
        List<JvmMetric> samples = List.of(metric(0.0, 1.0, 120, 0), metric(0.0, 1.0, 350, 0));
        assertEquals(350, JvmAlertService.aggregate(JvmAlertRule.THREAD_COUNT, samples), 0.001);
    }

    @Test
    void aggregatesFullGcDeltaFromCumulativeCounter() {
        // full_gc_count 为累计值：100 → 105 → 110，窗口增量 = 10
        List<JvmMetric> samples = List.of(
                metric(0.0, 1.0, 0, 100), metric(0.0, 1.0, 0, 105), metric(0.0, 1.0, 0, 110));
        assertEquals(10, JvmAlertService.aggregate(JvmAlertRule.FULL_GC_COUNT_DELTA, samples), 0.001);
    }

    @Test
    void fullGcDeltaNeverGoesNegativeOnCounterReset() {
        // 应用重启导致计数器回退（500 → 100）：应按时间序钳制为 0，
        // 而不是 max-min 算出 400 的虚假增量
        List<JvmMetric> samples = List.of(metric(0.0, 1.0, 0, 500), metric(0.0, 1.0, 0, 100));
        assertEquals(0, JvmAlertService.aggregate(JvmAlertRule.FULL_GC_COUNT_DELTA, samples), 0.001);
    }

    @Test
    void unknownMetricKeyYieldsNaN() {
        List<JvmMetric> samples = List.of(metric(1024.0, 2048.0, 10, 1));
        assertTrue(Double.isNaN(JvmAlertService.aggregate("unknown", samples)));
        assertTrue(Double.isNaN(JvmAlertService.aggregate(null, samples)));
    }

    // ---------------- 冷却期 ----------------

    @Test
    void quietPeriodSuppressesRepeatedAlerts() {
        JvmAlertRule rule = rule(JvmAlertRule.HEAP_USED_RATIO, 50, 300, 900);
        assertFalse(JvmAlertService.outOfQuietPeriod(rule) == false, "从未告警过应可告警");
        rule.setLastAlertTime(LocalDateTime.now().minusSeconds(60));
        assertFalse(JvmAlertService.outOfQuietPeriod(rule), "冷却期内应抑制");
        rule.setLastAlertTime(LocalDateTime.now().minusSeconds(901));
        assertTrue(JvmAlertService.outOfQuietPeriod(rule), "超出冷却期应放行");
    }

    @Test
    void zeroQuietPeriodAlwaysAllows() {
        JvmAlertRule rule = rule(JvmAlertRule.HEAP_USED_RATIO, 50, 300, 0);
        rule.setLastAlertTime(LocalDateTime.now());
        assertTrue(JvmAlertService.outOfQuietPeriod(rule));
    }

    // ---------------- 告警判定 ----------------

    @Test
    void sendsAlertWhenThresholdExceeded() {
        Fixture f = fixture(JvmAlertRule.HEAP_USED_RATIO, 50, samples(1800.0, 2048.0, 10, 0));

        boolean triggered = f.service.evaluate(f.rule);

        assertTrue(triggered);
        ArgumentCaptor<String> body = ArgumentCaptor.forClass(String.class);
        verify(f.notifier).send(nullable(String.class), contains("堆使用率超阈值"), body.capture());
        assertTrue(body.getValue().contains("87.89"), "告警正文应含实际值");
        assertTrue(body.getValue().contains("50.00"), "告警正文应含阈值");
        // 冷却期时间必须回写，否则下个周期会重复告警
        verify(f.ruleMapper).updateLastAlertTime(eq(f.rule.getId()), any(LocalDateTime.class));
    }

    @Test
    void doesNotAlertWhenBelowThreshold() {
        Fixture f = fixture(JvmAlertRule.HEAP_USED_RATIO, 90, samples(1024.0, 2048.0, 10, 0));
        assertFalse(f.service.evaluate(f.rule));
        verify(f.notifier, never()).send(any(), any(), any());
    }

    @Test
    void doesNotAlertWhenNoSamples() {
        Fixture f = fixture(JvmAlertRule.THREAD_COUNT, 1, List.of());
        assertFalse(f.service.evaluate(f.rule), "无采样（应用离线）不应误报");
        verify(f.notifier, never()).send(any(), any(), any());
    }

    @Test
    void disabledRuleIsSkipped() {
        Fixture f = fixture(JvmAlertRule.HEAP_USED_RATIO, 10, samples(1800.0, 2048.0, 10, 0));
        f.rule.setEnabled(0);
        assertFalse(f.service.evaluate(f.rule));
        verify(f.notifier, never()).send(any(), any(), any());
    }

    @Test
    void alertIsSuppressedWithinQuietPeriod() {
        Fixture f = fixture(JvmAlertRule.HEAP_USED_RATIO, 10, samples(1800.0, 2048.0, 10, 0));
        f.rule.setLastAlertTime(LocalDateTime.now().minusSeconds(30));
        assertFalse(f.service.evaluate(f.rule), "冷却期内应抑制告警");
        verify(f.notifier, never()).send(any(), any(), any());
    }

    @Test
    void notifyFailureDoesNotPropagate() {
        Fixture f = fixture(JvmAlertRule.HEAP_USED_RATIO, 10, samples(1800.0, 2048.0, 10, 0));
        doAnswer(inv -> {
            throw new RuntimeException("webhook down");
        }).when(f.notifier).send(any(), any(), any());

        // 通知失败不得抛出异常中断扫描
        assertTrue(f.service.evaluate(f.rule));
    }

    @Test
    void globalRuleUsesNullAppName() {
        Fixture f = fixture(JvmAlertRule.THREAD_COUNT, 100, samples(0.0, 1.0, 500, 0));
        f.rule.setAppName("");
        assertTrue(f.service.evaluate(f.rule));
        // 应用名为空串时应以 null 查询（匹配全部应用）
        verify(f.metricMapper).query(isNull(), any(LocalDateTime.class), any(LocalDateTime.class), anyInt());
    }

    // ---------------- 规则管理 ----------------

    @Test
    void rejectsInvalidMetricKey() {
        Fixture f = fixture(JvmAlertRule.HEAP_USED_RATIO, 10, List.of());
        f.rule.setMetricKey("nope");
        assertThrows(IllegalArgumentException.class, () -> f.service.add(f.rule));
    }

    @Test
    void rejectsNegativeThreshold() {
        Fixture f = fixture(JvmAlertRule.HEAP_USED_RATIO, -1, List.of());
        assertThrows(IllegalArgumentException.class, () -> f.service.add(f.rule));
    }

    @Test
    void rejectsOutOfRangeWindow() {
        Fixture f = fixture(JvmAlertRule.HEAP_USED_RATIO, 10, List.of());
        f.rule.setWindowSec(5);
        assertThrows(IllegalArgumentException.class, () -> f.service.add(f.rule));
        f.rule.setWindowSec(200_000);
        assertThrows(IllegalArgumentException.class, () -> f.service.add(f.rule));
    }

    @Test
    void updateKeepsUniqueKeyFields() {
        Fixture f = fixture(JvmAlertRule.HEAP_USED_RATIO, 10, List.of());
        JvmAlertRule existing = rule(JvmAlertRule.HEAP_USED_RATIO, 10, 300, 900);
        existing.setAppName("demo-app");
        when(f.ruleMapper.findById(any(Long.class))).thenReturn(existing);

        JvmAlertRule patch = new JvmAlertRule();
        patch.setId(1L);
        patch.setThreshold(95.0);
        // 试图改唯一键字段：应被服务端覆盖回原值，不产生脏数据
        patch.setAppName("other-app");
        patch.setMetricKey(JvmAlertRule.THREAD_COUNT);

        f.service.update(patch);

        assertEquals("demo-app", patch.getAppName());
        assertEquals(JvmAlertRule.HEAP_USED_RATIO, patch.getMetricKey());
    }

    @Test
    void updateRejectsMissingRule() {
        Fixture f = fixture(JvmAlertRule.HEAP_USED_RATIO, 10, List.of());
        when(f.ruleMapper.findById(any(Long.class))).thenReturn(null);
        JvmAlertRule patch = rule(JvmAlertRule.HEAP_USED_RATIO, 10, 300, 900);
        assertThrows(IllegalArgumentException.class, () -> f.service.update(patch));
    }

    @Test
    void setEnabledTogglesRule() {
        Fixture f = fixture(JvmAlertRule.HEAP_USED_RATIO, 10, List.of());
        JvmAlertRule existing = rule(JvmAlertRule.HEAP_USED_RATIO, 10, 300, 900);
        when(f.ruleMapper.findById(any(Long.class))).thenReturn(existing);

        f.service.setEnabled(1L, false);

        assertEquals(0, existing.getEnabled());
        verify(f.ruleMapper).update(existing);
    }

    @Test
    void metricOptionsExposeThreeKeys() {
        List<java.util.Map<String, String>> options = JvmAlertService.metricOptions();
        assertEquals(3, options.size());
        assertEquals("堆使用率", options.get(0).get("label"));
        assertEquals("%", options.get(0).get("unit"));
        assertEquals(JvmAlertRule.HEAP_USED_RATIO, options.get(0).get("key"));
    }

    // ---------------- 测试替身 ----------------

    private record Fixture(JvmAlertService service, JvmAlertRule rule,
                           JvmMetricMapper metricMapper, JvmAlertRuleMapper ruleMapper,
                           NotifyService notifier) {
    }

    private static Fixture fixture(String metricKey, double threshold, List<JvmMetric> samples) {
        JvmMetricMapper metricMapper = mock(JvmMetricMapper.class);
        JvmAlertRuleMapper ruleMapper = mock(JvmAlertRuleMapper.class);
        NotifyService notifier = mock(NotifyService.class);

        JvmAlertRule rule = rule(metricKey, threshold, 300, 900);
        rule.setId(1L);
        rule.setAppName("demo-app");

        when(metricMapper.query(nullable(String.class), any(LocalDateTime.class),
                any(LocalDateTime.class), anyInt())).thenReturn(samples);
        when(ruleMapper.findById(any(Long.class))).thenReturn(rule);

        return new Fixture(new JvmAlertService(ruleMapper, metricMapper, notifier),
                rule, metricMapper, ruleMapper, notifier);
    }

    private static JvmAlertRule rule(String metricKey, double threshold, int windowSec, int quietSec) {
        JvmAlertRule r = new JvmAlertRule();
        r.setMetricKey(metricKey);
        r.setThreshold(threshold);
        r.setWindowSec(windowSec);
        r.setQuietSec(quietSec);
        r.setEnabled(1);
        return r;
    }

    private static List<JvmMetric> samples(double heapUsed, double heapMax, int threads, long fullGc) {
        List<JvmMetric> list = new ArrayList<>();
        list.add(metric(heapUsed, heapMax, threads, fullGc));
        return list;
    }

    private static JvmMetric metric(double heapUsed, Double heapMax, int threads, long fullGc) {
        JvmMetric m = new JvmMetric();
        m.setAppName("demo-app");
        m.setCollectTime(LocalDateTime.now());
        m.setHeapUsedMb(heapUsed);
        m.setHeapMaxMb(heapMax);
        m.setThreadCount(threads);
        m.setFullGcCount(fullGc);
        return m;
    }
}
