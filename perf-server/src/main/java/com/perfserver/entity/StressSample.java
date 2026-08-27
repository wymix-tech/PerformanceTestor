package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 压测周期采样点：实时滚动图表数据（stress_sample 按天分区表）。
 */
public class StressSample {

    private Long id;
    private Long taskId;
    private LocalDateTime sampleTime;
    private Integer concurrency;
    private Double qps;
    private Double p50Ms;
    private Double p95Ms;
    private Double p99Ms;
    private Long errorCount;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public Long getTaskId() {
        return taskId;
    }

    public void setTaskId(Long taskId) {
        this.taskId = taskId;
    }

    public LocalDateTime getSampleTime() {
        return sampleTime;
    }

    public void setSampleTime(LocalDateTime sampleTime) {
        this.sampleTime = sampleTime;
    }

    public Integer getConcurrency() {
        return concurrency;
    }

    public void setConcurrency(Integer concurrency) {
        this.concurrency = concurrency;
    }

    public Double getQps() {
        return qps;
    }

    public void setQps(Double qps) {
        this.qps = qps;
    }

    public Double getP50Ms() {
        return p50Ms;
    }

    public void setP50Ms(Double p50Ms) {
        this.p50Ms = p50Ms;
    }

    public Double getP95Ms() {
        return p95Ms;
    }

    public void setP95Ms(Double p95Ms) {
        this.p95Ms = p95Ms;
    }

    public Double getP99Ms() {
        return p99Ms;
    }

    public void setP99Ms(Double p99Ms) {
        this.p99Ms = p99Ms;
    }

    public Long getErrorCount() {
        return errorCount;
    }

    public void setErrorCount(Long errorCount) {
        this.errorCount = errorCount;
    }
}
