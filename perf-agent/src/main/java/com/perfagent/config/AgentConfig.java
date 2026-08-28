package com.perfagent.config;

import java.util.HashMap;
import java.util.Map;

/**
 * Agent 配置：从 -javaagent 参数（k=v 逗号分隔）与 agent.properties 读取。
 *
 * <p>示例：-javaagent:perf-agent.jar=reportUrl=http://platform:8080/api/agent/report,appName=demo,sampleRate=0.1
 */
public class AgentConfig {

    /** 平台上报地址（多机部署指向平台服务器，如 http://platform-host:8080/api/agent/report） */
    public String reportUrl = "http://127.0.0.1:8080/api/agent/report";
    /** 应用名称（默认取 main 类名简化，或显式指定） */
    public String appName = "unknown-app";
    /**
     * 目标项目名（推荐显式指定，应与平台「源码仓库」的项目名一致）。
     * 平台侧按此名做准入校验：项目不存在时数据全部丢弃。
     * 未指定时回退用 appName 上报（兼容旧启动方式）。
     */
    public String targetProjectName = "";
    /** 调用链采样率（0~1，默认 0.1） */
    public double sampleRate = 0.1;
    /** 慢 SQL 阈值 ms（默认 500） */
    public long slowSqlThresholdMs = 500;
    /** JMX 采集周期秒（默认 5） */
    public long jmxCollectIntervalSec = 5;
    /** 上报批次大小（默认 500） */
    public int batchSize = 500;
    /** 上报缓冲区上限（超出丢弃最旧数据） */
    public int bufferSize = 5000;
    /** 是否启用方法埋点 */
    public boolean methodTraceEnabled = true;
    /** 是否启用 JDBC 拦截 */
    public boolean jdbcEnabled = true;
    /** 是否启用 JMX 采集 */
    public boolean jmxEnabled = true;

    /** 由 agentArgs 与默认值构造配置 */
    public static AgentConfig fromArgs(String agentArgs) {
        AgentConfig cfg = new AgentConfig();
        if (agentArgs == null || agentArgs.isBlank()) {
            return cfg;
        }
        Map<String, String> kv = new HashMap<>();
        for (String pair : agentArgs.split(",")) {
            int idx = pair.indexOf('=');
            if (idx > 0) {
                kv.put(pair.substring(0, idx).trim(), pair.substring(idx + 1).trim());
            }
        }
        if (kv.containsKey("reportUrl")) cfg.reportUrl = kv.get("reportUrl");
        if (kv.containsKey("appName")) cfg.appName = kv.get("appName");
        // 目标项目名：兼容 --target-project-name 与 targetProjectName 两种写法（k=v 均转小写比较由用户保证）
        if (kv.containsKey("targetProjectName")) cfg.targetProjectName = kv.get("targetProjectName");
        if (kv.containsKey("target-project-name")) cfg.targetProjectName = kv.get("target-project-name");
        if (kv.containsKey("sampleRate")) cfg.sampleRate = Double.parseDouble(kv.get("sampleRate"));
        if (kv.containsKey("slowSqlThresholdMs")) cfg.slowSqlThresholdMs = Long.parseLong(kv.get("slowSqlThresholdMs"));
        if (kv.containsKey("jmxCollectIntervalSec")) cfg.jmxCollectIntervalSec = Long.parseLong(kv.get("jmxCollectIntervalSec"));
        if (kv.containsKey("batchSize")) cfg.batchSize = Integer.parseInt(kv.get("batchSize"));
        if (kv.containsKey("bufferSize")) cfg.bufferSize = Integer.parseInt(kv.get("bufferSize"));
        if (kv.containsKey("methodTraceEnabled")) cfg.methodTraceEnabled = Boolean.parseBoolean(kv.get("methodTraceEnabled"));
        if (kv.containsKey("jdbcEnabled")) cfg.jdbcEnabled = Boolean.parseBoolean(kv.get("jdbcEnabled"));
        if (kv.containsKey("jmxEnabled")) cfg.jmxEnabled = Boolean.parseBoolean(kv.get("jmxEnabled"));
        return cfg;
    }
}
