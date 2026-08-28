package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 数据库连接配置：慢 SQL 分析时按 host/appName 匹配，
 * 用于执行 EXPLAIN、SHOW INDEX 等只读检测语句。
 */
public class DbConn {

    private Long id;
    private String connName;
    /** 主机标识（host:port/db，与慢 SQL dbHost 匹配） */
    private String host;
    /** 绑定应用（与慢 SQL appName 匹配，空 = 不限应用） */
    private String appName;
    /** JDBC URL（jdbc:mysql://...） */
    private String dbUrl;
    private String username;
    /** 内网演示明文存储，生产环境应加密 */
    private String password;
    private Integer isDefault;
    private LocalDateTime createTime;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getConnName() {
        return connName;
    }

    public void setConnName(String connName) {
        this.connName = connName;
    }

    public String getHost() {
        return host;
    }

    public void setHost(String host) {
        this.host = host;
    }

    public String getAppName() {
        return appName;
    }

    public void setAppName(String appName) {
        this.appName = appName;
    }

    public String getDbUrl() {
        return dbUrl;
    }

    public void setDbUrl(String dbUrl) {
        this.dbUrl = dbUrl;
    }

    public String getUsername() {
        return username;
    }

    public void setUsername(String username) {
        this.username = username;
    }

    public String getPassword() {
        return password;
    }

    public void setPassword(String password) {
        this.password = password;
    }

    public Integer getIsDefault() {
        return isDefault;
    }

    public void setIsDefault(Integer isDefault) {
        this.isDefault = isDefault;
    }

    public LocalDateTime getCreateTime() {
        return createTime;
    }

    public void setCreateTime(LocalDateTime createTime) {
        this.createTime = createTime;
    }
}
