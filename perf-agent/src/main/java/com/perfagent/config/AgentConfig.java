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
    /**
     * 被测应用 HTTP 端口（上报给平台，压测表单服务地址自动预填用）。
     * 0 = 未知；未配置时运行期尝试读 server.port 系统属性兜底。
     */
    public int appPort = 0;
    /**
     * 被测应用版本号（上报给平台，压测任务记录版本，支持不同版本性能比对）。
     * 留空时运行期自动从被测应用 jar 的 MANIFEST.MF 读 Implementation-Version。
     */
    public String appVersion = "";
    /**
     * 显式埋点包前缀（逗号分隔多个，如 "com.acme.core,com.acme.mall"）。
     * 留空时自动推导：被测应用 fat jar 的 Start-Class/Main-Class 所在包 = 业务根包（推荐）。
     * 覆盖业务包内全部类（Controller/Service/Repository/Component/工具类等），
     * 自动排除 JDK、第三方依赖与 agent 自身。
     */
    public String includePackages = "";
    /** 慢 SQL 阈值 ms（默认 500） */
    public long slowSqlThresholdMs = 500;
    /** JMX 采集周期秒（默认 5） */
    public long jmxCollectIntervalSec = 5;
    /** 上报批次大小（默认 500） */
    public int batchSize = 500;
    /** 上报缓冲区上限（超出丢弃最旧数据） */
    public int bufferSize = 5000;
    /**
     * trace 队列字节预算（估算值，超出丢最旧；大树调用链内存防爆）。
     * 估算公式：节点数 × 256B（Map+装箱+字符串的保守均值）。默认 8MB。
     */
    public int traceBufferBytes = 8 * 1024 * 1024;
    /**
     * 每秒最大采样 trace 条数（令牌桶洪峰保护，0=不限）。
     * 采样率控制比例，令牌桶控制绝对量：高 QPS 洪峰时避免 trace 洪水。默认 50。
     */
    public int maxTracesPerSec = 50;
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
        if (kv.containsKey("appPort")) cfg.appPort = Integer.parseInt(kv.get("appPort"));
        if (kv.containsKey("appVersion")) cfg.appVersion = kv.get("appVersion");
        if (kv.containsKey("includePackages")) cfg.includePackages = kv.get("includePackages");
        if (kv.containsKey("slowSqlThresholdMs")) cfg.slowSqlThresholdMs = Long.parseLong(kv.get("slowSqlThresholdMs"));
        if (kv.containsKey("jmxCollectIntervalSec")) cfg.jmxCollectIntervalSec = Long.parseLong(kv.get("jmxCollectIntervalSec"));
        if (kv.containsKey("batchSize")) cfg.batchSize = Integer.parseInt(kv.get("batchSize"));
        if (kv.containsKey("bufferSize")) cfg.bufferSize = Integer.parseInt(kv.get("bufferSize"));
        if (kv.containsKey("traceBufferBytes")) cfg.traceBufferBytes = Integer.parseInt(kv.get("traceBufferBytes"));
        if (kv.containsKey("maxTracesPerSec")) cfg.maxTracesPerSec = Integer.parseInt(kv.get("maxTracesPerSec"));
        if (kv.containsKey("methodTraceEnabled")) cfg.methodTraceEnabled = Boolean.parseBoolean(kv.get("methodTraceEnabled"));
        if (kv.containsKey("jdbcEnabled")) cfg.jdbcEnabled = Boolean.parseBoolean(kv.get("jdbcEnabled"));
        if (kv.containsKey("jmxEnabled")) cfg.jmxEnabled = Boolean.parseBoolean(kv.get("jmxEnabled"));
        return cfg;
    }
}
