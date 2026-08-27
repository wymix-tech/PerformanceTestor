package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 业务方法调用链：trace_json 保存 TraceNode 树（含每层耗时与占比）。
 */
public class TraceChain {

    private Long id;
    private String traceId;
    private String appName;
    /** 根方法（类#方法） */
    private String rootMethod;
    /** 调用树 JSON（TraceNode 树） */
    private String traceJson;
    private Double totalMs;
    private LocalDateTime startTime;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getTraceId() {
        return traceId;
    }

    public void setTraceId(String traceId) {
        this.traceId = traceId;
    }

    public String getAppName() {
        return appName;
    }

    public void setAppName(String appName) {
        this.appName = appName;
    }

    public String getRootMethod() {
        return rootMethod;
    }

    public void setRootMethod(String rootMethod) {
        this.rootMethod = rootMethod;
    }

    public String getTraceJson() {
        return traceJson;
    }

    public void setTraceJson(String traceJson) {
        this.traceJson = traceJson;
    }

    public Double getTotalMs() {
        return totalMs;
    }

    public void setTotalMs(Double totalMs) {
        this.totalMs = totalMs;
    }

    public LocalDateTime getStartTime() {
        return startTime;
    }

    public void setStartTime(LocalDateTime startTime) {
        this.startTime = startTime;
    }
}
