package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 方法级 CPU 热点：agent HotspotSampler 弱采样聚合上报（RUNNABLE 栈顶帧命中数）。
 */
public class HotspotMethod {

    private Long id;
    private String appName;
    /** 方法全限定名（class#method） */
    private String methodName;
    /** 采样命中次数（周期内帧命中计数） */
    private Long samples;
    private LocalDateTime sampleTime;

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

    public String getMethodName() {
        return methodName;
    }

    public void setMethodName(String methodName) {
        this.methodName = methodName;
    }

    public Long getSamples() {
        return samples;
    }

    public void setSamples(Long samples) {
        this.samples = samples;
    }

    public LocalDateTime getSampleTime() {
        return sampleTime;
    }

    public void setSampleTime(LocalDateTime sampleTime) {
        this.sampleTime = sampleTime;
    }
}
