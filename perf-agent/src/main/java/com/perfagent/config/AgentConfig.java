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
    /**
     * 是否启用中间件拦截（Redis / ES / RabbitMQ，默认 false）。
     * 中间件命令调用极高频（Redis 单次请求可达数十次），默认关闭以控制常态开销；
     * 需要定位中间件慢命令时显式开启 middlewareEnabled=true。
     */
    public boolean middlewareEnabled = false;
    /**
     * 慢命令阈值 ms（默认 100）。中间件单次命令通常远快于 SQL
     * （Redis 正常 <1ms），故默认阈值比慢 SQL 更严格。
     */
    public long slowCommandThresholdMs = 100;
    /** 是否启用 JMX 采集 */
    public boolean jmxEnabled = true;
    /**
     * 是否启用跨线程异步上下文透传（P1-5，默认 false）：
     * 拦截 {@code Executor#execute(Runnable)} 包装任务，子线程恢复调用链与压测染色。
     * 默认关闭：验证稳定后再开启（改造面涉及线程池提交路径，需灰度观察）。
     */
    public boolean asyncTraceEnabled = false;
    /**
     * 是否启用 Servlet Filter 根模式（默认 true）：以最外层 Filter#doFilter 作为
     * 调用链唯一采样决策点，消除采样稀释与半截链路。非 Servlet 应用无匹配类，零影响。
     */
    public boolean filterRootEnabled = true;
    /**
     * 是否启用 HTTP 客户端出口拦截（P2-2，默认 false）：在请求发出前注入
     * {@code traceparent}（W3C 跨服务串联）与 {@code X-Perf-Stress-Task-Id}（染色透传），
     * 支持 Apache HttpClient / OkHttp / Spring ClientHttpRequest / JDK HttpClient。
     * 默认关闭：需要微服务串联时显式开启（反射注入有极小开销，单体应用无需）。
     */
    public boolean httpClientEnabled = false;
    /** 是否启用方法级 CPU 热点采样（默认 false，需要时开启控制常态开销） */
    public boolean hotspotEnabled = false;
    /**
     * CPU 热点采样周期毫秒（默认 50ms）。下限 20ms（HotspotSampler 防呆强制）：
     * 高频全线程 getThreadInfo 会触发 Safepoint 风暴，影响被测应用停顿；
     * P2 预留替换为 JDK Flight Recorder（JFR）连续剖析。
     */
    public int hotspotIntervalMs = 50;
    /** CPU 热点聚合上报周期秒 */
    public int hotspotReportSec = 30;
    /** 每次上报的热点方法数上限 */
    public int hotspotTopN = 50;
    /**
     * 热点剖析模式（hotspotEnabled=true 时生效，P2-1）：
     * jfr（默认，JDK 17 内置 JFR 连续剖析，低开销三维度）/ threadmx（旧 ThreadMXBean 弱采样，
     * 兼容回退）/ off（关闭剖析，只留调用链）。
     */
    public String profilerMode = "jfr";
    /** JFR ExecutionSample 采样周期毫秒（默认 20，CPU 维度） */
    public int jfrCpuIntervalMs = 20;
    /** JFR ObjectAllocationSample 节流（次/秒，默认 150，对象分配维度） */
    public int jfrAllocThrottle = 150;
    /** JFR JavaMonitorEnter 阈值毫秒（默认 10，锁竞争维度） */
    public long jfrLockThresholdMs = 10;
    /**
     * 探针上报令牌（agentToken=&lt;值&gt;）：与平台 perf.auth.agent-token 保持一致时，
     * 上报请求会携带 X-Perf-Agent-Token 头。留空则不携带（平台未配置令牌时保持兼容）。
     */
    public String agentToken = "";

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
        if (kv.containsKey("middlewareEnabled")) cfg.middlewareEnabled = Boolean.parseBoolean(kv.get("middlewareEnabled"));
        if (kv.containsKey("slowCommandThresholdMs")) cfg.slowCommandThresholdMs = Long.parseLong(kv.get("slowCommandThresholdMs"));
        if (kv.containsKey("jmxEnabled")) cfg.jmxEnabled = Boolean.parseBoolean(kv.get("jmxEnabled"));
        if (kv.containsKey("asyncTraceEnabled")) cfg.asyncTraceEnabled = Boolean.parseBoolean(kv.get("asyncTraceEnabled"));
        if (kv.containsKey("filterRootEnabled")) cfg.filterRootEnabled = Boolean.parseBoolean(kv.get("filterRootEnabled"));
        if (kv.containsKey("httpClientEnabled")) cfg.httpClientEnabled = Boolean.parseBoolean(kv.get("httpClientEnabled"));
        if (kv.containsKey("hotspotEnabled")) cfg.hotspotEnabled = Boolean.parseBoolean(kv.get("hotspotEnabled"));
        if (kv.containsKey("hotspotIntervalMs")) cfg.hotspotIntervalMs = Integer.parseInt(kv.get("hotspotIntervalMs"));
        if (kv.containsKey("hotspotReportSec")) cfg.hotspotReportSec = Integer.parseInt(kv.get("hotspotReportSec"));
        if (kv.containsKey("hotspotTopN")) cfg.hotspotTopN = Integer.parseInt(kv.get("hotspotTopN"));
        if (kv.containsKey("profilerMode")) cfg.profilerMode = kv.get("profilerMode");
        if (kv.containsKey("jfrCpuIntervalMs")) cfg.jfrCpuIntervalMs = Integer.parseInt(kv.get("jfrCpuIntervalMs"));
        if (kv.containsKey("jfrAllocThrottle")) cfg.jfrAllocThrottle = Integer.parseInt(kv.get("jfrAllocThrottle"));
        if (kv.containsKey("jfrLockThresholdMs")) cfg.jfrLockThresholdMs = Long.parseLong(kv.get("jfrLockThresholdMs"));
        if (kv.containsKey("agentToken")) cfg.agentToken = kv.get("agentToken");
        return cfg;
    }
}
