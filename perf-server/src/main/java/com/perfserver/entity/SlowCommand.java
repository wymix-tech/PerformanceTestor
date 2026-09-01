package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 中间件慢命令记录：agent 拦截 Redis / ES / RabbitMQ 命令，超阈值上报后写入。
 *
 * <p>与 {@link SlowSql} 平行：慢 SQL 聚焦数据库，本表聚焦中间件，
 * 两者通过 traceId 与同一条调用链关联，前端合并为统一慢命令视图。
 */
public class SlowCommand {

    /** 类型：redis / es / mq */
    public static final String TYPE_REDIS = "redis";
    public static final String TYPE_ES = "es";
    public static final String TYPE_MQ = "mq";

    private Long id;
    private String appName;
    /** 中间件类型：redis / es / mq */
    private String type;
    /** 命令文本（方法名 + 参数，已截断；如 "get user:123" / "basicPublish order.ex order.new <142B> ..."） */
    private String command;
    private Double costMs;
    /** 关联调用链 ID（未采样时为空） */
    private String traceId;
    /** 触发方法链 JSON 数组（根 → 叶），用于源码精确定位 */
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

    public String getType() {
        return type;
    }

    public void setType(String type) {
        this.type = type;
    }

    public String getCommand() {
        return command;
    }

    public void setCommand(String command) {
        this.command = command;
    }

    public Double getCostMs() {
        return costMs;
    }

    public void setCostMs(Double costMs) {
        this.costMs = costMs;
    }

    public String getTraceId() {
        return traceId;
    }

    public void setTraceId(String traceId) {
        this.traceId = traceId;
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
