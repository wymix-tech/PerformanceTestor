package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 慢 SQL 记录：JDBC 拦截与 MySQL 慢查询日志解析双通道写入。
 */
public class SlowSql {

    private Long id;
    private String appName;
    /** SQL 文本（已脱敏，参数以 ? 占位） */
    private String sqlText;
    private Double costMs;
    /** 来源：jdbc 拦截 / slowlog 日志 */
    private String source;
    private String dbHost;
    /** 触发方法链 JSON 数组（根 → 叶，如 Controller → Service → Mapper），用于源码精确定位 */
    private String methodChain;
    private LocalDateTime executeTime;

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

    public String getSqlText() {
        return sqlText;
    }

    public void setSqlText(String sqlText) {
        this.sqlText = sqlText;
    }

    public Double getCostMs() {
        return costMs;
    }

    public void setCostMs(Double costMs) {
        this.costMs = costMs;
    }

    public String getSource() {
        return source;
    }

    public void setSource(String source) {
        this.source = source;
    }

    public String getDbHost() {
        return dbHost;
    }

    public void setDbHost(String dbHost) {
        this.dbHost = dbHost;
    }

    public String getMethodChain() {
        return methodChain;
    }

    public void setMethodChain(String methodChain) {
        this.methodChain = methodChain;
    }

    public LocalDateTime getExecuteTime() {
        return executeTime;
    }

    public void setExecuteTime(LocalDateTime executeTime) {
        this.executeTime = executeTime;
    }
}
