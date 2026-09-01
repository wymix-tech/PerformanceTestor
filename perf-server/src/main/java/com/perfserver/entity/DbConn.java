package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 数据库连接配置：慢 SQL 分析时按 host/appName 匹配，
 * 用于执行 EXPLAIN、SHOW INDEX 等只读检测语句。
 *
 * <p>安全约束：{@code password} 经 {@link com.perfserver.security.CryptoService}
 * 加密后落库（{@code ENC:} 前缀），仅列表接口返回脱敏值 + {@code hasPassword} 标记；
 * 明文只在真正建立 JDBC 连接的取用点解密，不进入日志、AI 上下文与接口出口。
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
    /** 数据库密码：加密存储（ENC: 前缀 + AES-GCM），列表接口返回脱敏值 */
    private String password;
    private Integer isDefault;
    private LocalDateTime createTime;
    /** 是否已配置密码（脱敏输出用，前端据此区分「已配置/未配置」；不落库） */
    private Boolean hasPassword;

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

    public Boolean getHasPassword() {
        return hasPassword;
    }

    public void setHasPassword(Boolean hasPassword) {
        this.hasPassword = hasPassword;
    }
}
