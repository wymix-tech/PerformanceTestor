package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 定时压测调度：周期执行同场景压测 + 版本基线对比 + 超阈值 webhook 告警（回归门禁）。
 */
public class StressSchedule {

    private Long id;
    private String scheduleName;
    /** 压测任务模板 JSON（StressTaskRequest，含 targetUrl/concurrency/durationSec 等） */
    private String taskJson;
    /** 执行间隔（分钟） */
    private Integer intervalMinutes;
    /** 是否启用：1 是 0 否 */
    private Integer enabled;
    /** 基线任务 ID（0 = 自动取同应用同 URL 最近完成任务） */
    private Long baselineTaskId;
    /** 告警 webhook（钉钉/飞书/自定义，空 = 不告警） */
    private String webhookUrl;
    /** 上次触发任务 ID（完成后做基线对比） */
    private Long lastTaskId;
    private LocalDateTime lastRunTime;
    private LocalDateTime nextRunTime;
    private LocalDateTime createTime;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getScheduleName() {
        return scheduleName;
    }

    public void setScheduleName(String scheduleName) {
        this.scheduleName = scheduleName;
    }

    public String getTaskJson() {
        return taskJson;
    }

    public void setTaskJson(String taskJson) {
        this.taskJson = taskJson;
    }

    public Integer getIntervalMinutes() {
        return intervalMinutes;
    }

    public void setIntervalMinutes(Integer intervalMinutes) {
        this.intervalMinutes = intervalMinutes;
    }

    public Integer getEnabled() {
        return enabled;
    }

    public void setEnabled(Integer enabled) {
        this.enabled = enabled;
    }

    public Long getBaselineTaskId() {
        return baselineTaskId;
    }

    public void setBaselineTaskId(Long baselineTaskId) {
        this.baselineTaskId = baselineTaskId;
    }

    public String getWebhookUrl() {
        return webhookUrl;
    }

    public void setWebhookUrl(String webhookUrl) {
        this.webhookUrl = webhookUrl;
    }

    public Long getLastTaskId() {
        return lastTaskId;
    }

    public void setLastTaskId(Long lastTaskId) {
        this.lastTaskId = lastTaskId;
    }

    public LocalDateTime getLastRunTime() {
        return lastRunTime;
    }

    public void setLastRunTime(LocalDateTime lastRunTime) {
        this.lastRunTime = lastRunTime;
    }

    public LocalDateTime getNextRunTime() {
        return nextRunTime;
    }

    public void setNextRunTime(LocalDateTime nextRunTime) {
        this.nextRunTime = nextRunTime;
    }

    public LocalDateTime getCreateTime() {
        return createTime;
    }

    public void setCreateTime(LocalDateTime createTime) {
        this.createTime = createTime;
    }
}
