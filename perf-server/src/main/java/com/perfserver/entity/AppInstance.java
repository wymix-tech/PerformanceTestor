package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 被测应用实例：agent 首次上报自动注册，每次上报刷新心跳。
 */
public class AppInstance {

    private Long id;
    private String appName;
    private String appHost;
    private String agentVersion;
    /** 状态：1 在线 0 离线 */
    private Integer status;
    private LocalDateTime lastReportTime;
    private LocalDateTime createTime;

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

    public String getAppHost() {
        return appHost;
    }

    public void setAppHost(String appHost) {
        this.appHost = appHost;
    }

    public String getAgentVersion() {
        return agentVersion;
    }

    public void setAgentVersion(String agentVersion) {
        this.agentVersion = agentVersion;
    }

    public Integer getStatus() {
        return status;
    }

    public void setStatus(Integer status) {
        this.status = status;
    }

    public LocalDateTime getLastReportTime() {
        return lastReportTime;
    }

    public void setLastReportTime(LocalDateTime lastReportTime) {
        this.lastReportTime = lastReportTime;
    }

    public LocalDateTime getCreateTime() {
        return createTime;
    }

    public void setCreateTime(LocalDateTime createTime) {
        this.createTime = createTime;
    }
}
