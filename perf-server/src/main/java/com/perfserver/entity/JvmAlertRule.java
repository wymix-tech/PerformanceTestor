package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * JVM 指标阈值告警规则（jvm_alert_rule 表）：持续性能守护的规则配置。
 *
 * <p>指标键见 {@link MetricKey}：堆使用率、Full GC 次数增量、线程数。
 * 冷却期（{@code quietSec}）用于抑制告警风暴——同一规则在冷却期内不重复发送。
 */
public class JvmAlertRule {

    /** 堆使用率 %：窗口内 heap_used_mb / heap_max_mb 的最大值 */
    public static final String HEAP_USED_RATIO = "heapUsedRatio";
    /** Full GC 次数增量：窗口内 full_gc_count 的增量（累计值相减） */
    public static final String FULL_GC_COUNT_DELTA = "fullGcCountDelta";
    /** 线程数：窗口内 thread_count 的最大值 */
    public static final String THREAD_COUNT = "threadCount";

    private Long id;
    /** 应用名（空串表示全部应用） */
    private String appName = "";
    private String metricKey;
    private Double threshold;
    private Integer windowSec = 300;
    private Integer quietSec = 900;
    private Integer enabled = 1;
    private String webhook;
    private LocalDateTime lastAlertTime;
    private LocalDateTime createTime;

    /** 指标展示名（供告警文案与前端下拉使用） */
    public static String metricLabel(String metricKey) {
        if (metricKey == null) {
            return "";
        }
        return switch (metricKey) {
            case HEAP_USED_RATIO -> "堆使用率";
            case FULL_GC_COUNT_DELTA -> "Full GC 次数增量";
            case THREAD_COUNT -> "线程数";
            default -> metricKey;
        };
    }

    /** 指标单位（供告警文案拼接） */
    public static String metricUnit(String metricKey) {
        if (metricKey == null) {
            return "";
        }
        return switch (metricKey) {
            case HEAP_USED_RATIO -> "%";
            case FULL_GC_COUNT_DELTA -> " 次";
            case THREAD_COUNT -> " 个";
            default -> "";
        };
    }

    /** 是否为合法指标键 */
    public static boolean isValidMetric(String metricKey) {
        return HEAP_USED_RATIO.equals(metricKey)
                || FULL_GC_COUNT_DELTA.equals(metricKey)
                || THREAD_COUNT.equals(metricKey);
    }

    /** 是否启用 */
    public boolean isEnabled() {
        return enabled != null && enabled == 1;
    }

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getAppName() {
        return appName;
    }

    public void setAppName(String appName) {
        this.appName = appName == null ? "" : appName;
    }

    public String getMetricKey() {
        return metricKey;
    }

    public void setMetricKey(String metricKey) {
        this.metricKey = metricKey;
    }

    public Double getThreshold() {
        return threshold;
    }

    public void setThreshold(Double threshold) {
        this.threshold = threshold;
    }

    public Integer getWindowSec() {
        return windowSec;
    }

    public void setWindowSec(Integer windowSec) {
        this.windowSec = windowSec;
    }

    public Integer getQuietSec() {
        return quietSec;
    }

    public void setQuietSec(Integer quietSec) {
        this.quietSec = quietSec;
    }

    public Integer getEnabled() {
        return enabled;
    }

    public void setEnabled(Integer enabled) {
        this.enabled = enabled;
    }

    public String getWebhook() {
        return webhook;
    }

    public void setWebhook(String webhook) {
        this.webhook = webhook;
    }

    public LocalDateTime getLastAlertTime() {
        return lastAlertTime;
    }

    public void setLastAlertTime(LocalDateTime lastAlertTime) {
        this.lastAlertTime = lastAlertTime;
    }

    public LocalDateTime getCreateTime() {
        return createTime;
    }

    public void setCreateTime(LocalDateTime createTime) {
        this.createTime = createTime;
    }
}
