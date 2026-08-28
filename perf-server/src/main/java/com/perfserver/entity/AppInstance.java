package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 被测应用实例：agent 首次上报自动注册，每次上报刷新心跳。
 */
public class AppInstance {

    private Long id;
    private String appName;
    private String appHost;
    /** 应用 HTTP 端口（agent 上报，压测表单服务地址自动预填用；null=未知） */
    private Integer appPort;
    /** 应用版本号（agent 上报或探测 jar MANIFEST，压测任务快照记录，支持版本间比对） */
    private String appVersion;
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

    public Integer getAppPort() {
        return appPort;
    }

    public void setAppPort(Integer appPort) {
        this.appPort = appPort;
    }

    public String getAppVersion() {
        return appVersion;
    }

    public void setAppVersion(String appVersion) {
        this.appVersion = appVersion;
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
