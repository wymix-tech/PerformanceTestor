package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * JVM 指标记录：对应 jvm_metric 分区表，字段与 agent 上报格式对齐。
 */
public class JvmMetric {

    private Long id;
    private String appName;
    private LocalDateTime collectTime;
    private Double heapUsedMb;
    private Double heapMaxMb;
    private Double nonHeapUsedMb;
    private Long youngGcCount;
    private Long youngGcMs;
    private Long fullGcCount;
    private Long fullGcMs;
    private Integer threadCount;
    private Double cpuUsage;
    private Double systemCpu;

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
        this.appName = appName;
    }

    public LocalDateTime getCollectTime() {
        return collectTime;
    }

    public void setCollectTime(LocalDateTime collectTime) {
        this.collectTime = collectTime;
    }

    public Double getHeapUsedMb() {
        return heapUsedMb;
    }

    public void setHeapUsedMb(Double heapUsedMb) {
        this.heapUsedMb = heapUsedMb;
    }

    public Double getHeapMaxMb() {
        return heapMaxMb;
    }

    public void setHeapMaxMb(Double heapMaxMb) {
        this.heapMaxMb = heapMaxMb;
    }

    public Double getNonHeapUsedMb() {
        return nonHeapUsedMb;
    }

    public void setNonHeapUsedMb(Double nonHeapUsedMb) {
        this.nonHeapUsedMb = nonHeapUsedMb;
    }

    public Long getYoungGcCount() {
        return youngGcCount;
    }

    public void setYoungGcCount(Long youngGcCount) {
        this.youngGcCount = youngGcCount;
    }

    public Long getYoungGcMs() {
        return youngGcMs;
    }

    public void setYoungGcMs(Long youngGcMs) {
        this.youngGcMs = youngGcMs;
    }

    public Long getFullGcCount() {
        return fullGcCount;
    }

    public void setFullGcCount(Long fullGcCount) {
        this.fullGcCount = fullGcCount;
    }

    public Long getFullGcMs() {
        return fullGcMs;
    }

    public void setFullGcMs(Long fullGcMs) {
        this.fullGcMs = fullGcMs;
    }

    public Integer getThreadCount() {
        return threadCount;
    }

    public void setThreadCount(Integer threadCount) {
        this.threadCount = threadCount;
    }

    public Double getCpuUsage() {
        return cpuUsage;
    }

    public void setCpuUsage(Double cpuUsage) {
        this.cpuUsage = cpuUsage;
    }

    public Double getSystemCpu() {
        return systemCpu;
    }

    public void setSystemCpu(Double systemCpu) {
        this.systemCpu = systemCpu;
    }
}
