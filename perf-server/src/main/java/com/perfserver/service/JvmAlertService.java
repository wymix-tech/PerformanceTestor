package com.perfserver.service;

import com.perfserver.entity.JvmAlertRule;
import com.perfserver.entity.JvmMetric;
import com.perfserver.mapper.JvmAlertRuleMapper;
import com.perfserver.mapper.JvmMetricMapper;
import com.perfserver.notify.NotifyService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * JVM 指标阈值告警：定时扫描启用中的规则，按窗口聚合指标，超阈值即告警。
 *
 * <p>设计要点：
 * <ul>
 *   <li><b>指标聚合</b>：堆使用率与线程数取窗口内最大值（瞬时尖刺也有意义）；
 *       Full GC 取窗口内 {@code full_gc_count} 的增量（该字段是累计值，需首尾相减）。</li>
 *   <li><b>冷却期抑制</b>：同一规则两次告警间隔不小于 {@code quietSec}，
 *       避免持续超阈值时每个扫描周期都发一条，刷屏并耗尽 webhook 配额。</li>
 *   <li><b>尽力而为</b>：单条规则判定异常（如 webhook 不可达）不影响其他规则，
 *       且通知失败只记日志，不阻断扫描。</li>
 *   <li><b>无数据不告警</b>：窗口内无采样点（应用离线/agent 未接入）直接跳过，
 *       不因「读不到数据」误报。</li>
 * </ul>
 */
@Service
public class JvmAlertService {

    private static final Logger log = LoggerFactory.getLogger(JvmAlertService.class);

    /** 单次扫描最多拉取的采样点（防止超大窗口把整表读进内存） */
    private static final int MAX_SAMPLES = 5000;

    private final JvmAlertRuleMapper ruleMapper;
    private final JvmMetricMapper metricMapper;
    private final NotifyService notifyService;

    public JvmAlertService(JvmAlertRuleMapper ruleMapper, JvmMetricMapper metricMapper,
                           NotifyService notifyService) {
        this.ruleMapper = ruleMapper;
        this.metricMapper = metricMapper;
        this.notifyService = notifyService;
    }

    /**
     * 定时扫描全部启用规则并发送告警（每 60 秒一次）。
     *
     * <p>扫描周期固定：各规则用自己的 {@code windowSec} 决定回看窗口，
     * 冷却期决定是否真的发送，互不干扰。
     */
    @Scheduled(fixedDelay = 60_000)
    public void scanAndAlert() {
        List<JvmAlertRule> rules;
        try {
            rules = ruleMapper.listEnabled();
        } catch (Exception e) {
            log.warn("jvm alert: 加载告警规则失败", e);
            return;
        }
        if (rules == null || rules.isEmpty()) {
            return;
        }
        for (JvmAlertRule rule : rules) {
            try {
                evaluate(rule);
            } catch (Exception e) {
                // 单条规则异常不得中断整轮扫描
                log.warn("jvm alert: 规则 #{} 判定异常", rule.getId(), e);
            }
        }
    }

    /**
     * 判定单条规则：聚合窗口指标 → 超阈值且在冷却期外 → 发送告警并回写时间。
     *
     * @return 本次是否触发告警（供测试与手动触发使用）
     */
    public boolean evaluate(JvmAlertRule rule) {
        if (rule == null || !rule.isEnabled() || rule.getThreshold() == null) {
            return false;
        }
        int windowSec = rule.getWindowSec() == null || rule.getWindowSec() <= 0
                ? 300 : rule.getWindowSec();
        LocalDateTime end = LocalDateTime.now();
        LocalDateTime start = end.minusSeconds(windowSec);

        String appName = rule.getAppName() == null || rule.getAppName().isBlank()
                ? null : rule.getAppName();
        List<JvmMetric> samples = metricMapper.query(appName, start, end, MAX_SAMPLES);
        if (samples == null || samples.isEmpty()) {
            return false; // 无采样：应用可能离线，不告警
        }

        double actual = aggregate(rule.getMetricKey(), samples);
        if (Double.isNaN(actual)) {
            return false; // 指标键不可识别或无有效值：跳过，不误报
        }
        if (actual <= rule.getThreshold()) {
            return false;
        }
        if (!outOfQuietPeriod(rule)) {
            return false; // 冷却期内抑制，避免告警风暴
        }

        String title = "JVM 告警：" + (appName == null ? "全部应用" : appName)
                + " " + JvmAlertRule.metricLabel(rule.getMetricKey()) + "超阈值";
        String text = String.format("%s %s 当前 %.2f%s，阈值 %.2f%s（统计窗口 %ds）",
                appName == null ? "全部应用" : appName,
                JvmAlertRule.metricLabel(rule.getMetricKey()),
                actual, JvmAlertRule.metricUnit(rule.getMetricKey()),
                rule.getThreshold(), JvmAlertRule.metricUnit(rule.getMetricKey()),
                windowSec);
        try {
            notifyService.send(rule.getWebhook(), title, text);
        } catch (Exception e) {
            // 通知失败只记日志：告警尽力而为，不影响其他规则与后续扫描
            log.warn("jvm alert: 规则 #{} 通知发送失败", rule.getId(), e);
        }
        LocalDateTime now = LocalDateTime.now();
        rule.setLastAlertTime(now);
        try {
            ruleMapper.updateLastAlertTime(rule.getId(), now);
        } catch (Exception e) {
            log.warn("jvm alert: 规则 #{} 回写告警时间失败（可能导致重复告警）", rule.getId(), e);
        }
        log.info("jvm alert triggered: ruleId={}, app={}, metric={}, actual={}, threshold={}",
                rule.getId(), appName, rule.getMetricKey(), actual, rule.getThreshold());
        return true;
    }

    /**
     * 按指标键聚合窗口内采样。
     *
     * @return 聚合值；{@link Double#NaN} 表示指标键不可识别或无可聚合数据
     */
    static double aggregate(String metricKey, List<JvmMetric> samples) {
        if (metricKey == null) {
            return Double.NaN;
        }
        switch (metricKey) {
            case JvmAlertRule.HEAP_USED_RATIO -> {
                double max = -1;
                for (JvmMetric m : samples) {
                    Double used = m.getHeapUsedMb();
                    Double maxMb = m.getHeapMaxMb();
                    if (used == null || maxMb == null || maxMb <= 0) {
                        continue;
                    }
                    max = Math.max(max, used * 100.0 / maxMb);
                }
                return max < 0 ? Double.NaN : max;
            }
            case JvmAlertRule.THREAD_COUNT -> {
                int max = -1;
                for (JvmMetric m : samples) {
                    Integer t = m.getThreadCount();
                    if (t != null) {
                        max = Math.max(max, t);
                    }
                }
                return max < 0 ? Double.NaN : max;
            }
            case JvmAlertRule.FULL_GC_COUNT_DELTA -> {
                // full_gc_count 是累计值：必须按时间序取「末 - 初」。
                // 用 max-min 会在应用重启导致计数器回退时算出巨大虚增（如 500→100 得 400）。
                Long first = null;
                Long last = null;
                for (JvmMetric m : samples) {
                    Long c = m.getFullGcCount();
                    if (c == null) {
                        continue;
                    }
                    if (first == null) {
                        first = c;
                    }
                    last = c;
                }
                if (first == null || last == null) {
                    return Double.NaN;
                }
                // 计数器回退（应用重启）时钳制为 0，不产生负数或虚假大数值告警
                return Math.max(0, last - first);
            }
            default -> {
                return Double.NaN;
            }
        }
    }

    /** 是否超出冷却期（从未告警过则视为可告警） */
    static boolean outOfQuietPeriod(JvmAlertRule rule) {
        if (rule.getLastAlertTime() == null) {
            return true;
        }
        int quietSec = rule.getQuietSec() == null || rule.getQuietSec() <= 0 ? 0 : rule.getQuietSec();
        if (quietSec == 0) {
            return true;
        }
        return Duration.between(rule.getLastAlertTime(), LocalDateTime.now()).getSeconds() >= quietSec;
    }

    // ---------------- 规则管理 ----------------

    /** 规则列表（appName 为空查全部） */
    public List<JvmAlertRule> list(String appName, int limit) {
        return ruleMapper.list(appName == null ? "" : appName, limit);
    }

    /** 新增规则（校验指标键与阈值） */
    public JvmAlertRule add(JvmAlertRule rule) {
        validate(rule);
        ruleMapper.insert(rule);
        return rule;
    }

    /** 更新规则（阈值/窗口/冷却/启用/通知地址） */
    public JvmAlertRule update(JvmAlertRule rule) {
        validate(rule);
        if (rule.getId() == null) {
            throw new IllegalArgumentException("规则 ID 不能为空");
        }
        JvmAlertRule existing = ruleMapper.findById(rule.getId());
        if (existing == null) {
            throw new IllegalArgumentException("告警规则不存在：" + rule.getId());
        }
        // appName / metricKey 属于唯一键，不允许改（避免绕过唯一约束）
        rule.setAppName(existing.getAppName());
        rule.setMetricKey(existing.getMetricKey());
        ruleMapper.update(rule);
        return rule;
    }

    /** 删除规则 */
    public void delete(long id) {
        ruleMapper.delete(id);
    }

    /** 启用/停用开关（看板直接切换，无需提交整条规则） */
    public void setEnabled(long id, boolean enabled) {
        JvmAlertRule existing = ruleMapper.findById(id);
        if (existing == null) {
            throw new IllegalArgumentException("告警规则不存在：" + id);
        }
        existing.setEnabled(enabled ? 1 : 0);
        ruleMapper.update(existing);
    }

    /** 手动触发一次判定（看板「立即检测」用） */
    public boolean checkNow(long id) {
        JvmAlertRule rule = ruleMapper.findById(id);
        if (rule == null) {
            throw new IllegalArgumentException("告警规则不存在：" + id);
        }
        return evaluate(rule);
    }

    /** 可用的指标键（前端下拉用） */
    public static List<Map<String, String>> metricOptions() {
        List<Map<String, String>> options = new ArrayList<>();
        for (String key : List.of(JvmAlertRule.HEAP_USED_RATIO,
                JvmAlertRule.FULL_GC_COUNT_DELTA, JvmAlertRule.THREAD_COUNT)) {
            Map<String, String> option = new LinkedHashMap<>();
            option.put("key", key);
            option.put("label", JvmAlertRule.metricLabel(key));
            option.put("unit", JvmAlertRule.metricUnit(key));
            options.add(option);
        }
        return options;
    }

    private static void validate(JvmAlertRule rule) {
        if (rule == null) {
            throw new IllegalArgumentException("规则不能为空");
        }
        if (!JvmAlertRule.isValidMetric(rule.getMetricKey())) {
            throw new IllegalArgumentException("不支持的指标键：" + rule.getMetricKey()
                    + "（可选 heapUsedRatio / fullGcCountDelta / threadCount）");
        }
        if (rule.getThreshold() == null || rule.getThreshold() < 0) {
            throw new IllegalArgumentException("阈值必须为不小于 0 的数值");
        }
        if (rule.getWindowSec() != null && (rule.getWindowSec() < 30 || rule.getWindowSec() > 86400)) {
            throw new IllegalArgumentException("统计窗口必须在 30-86400 秒之间");
        }
        if (rule.getQuietSec() != null && (rule.getQuietSec() < 0 || rule.getQuietSec() > 86400)) {
            throw new IllegalArgumentException("冷却期必须在 0-86400 秒之间");
        }
    }
}
