package com.perfagent;

import com.perfagent.config.AgentConfig;
import com.perfagent.instrumentation.JdbcInterceptor;
import com.perfagent.instrumentation.TraceEnhancer;
import com.perfagent.jmx.JvmCollector;
import com.perfagent.reporter.HttpReporter;
import com.perfagent.reporter.MetricsReporter;
import com.perfagent.trace.TraceContext;
import com.perfagent.trace.TraceSampler;

import java.lang.instrument.Instrumentation;

/**
 * perf-agent 入口（premain / agentmain）。
 *
 * <p>职责：
 * <ol>
 *   <li>解析 -javaagent 参数（reportUrl/appName/sampleRate/slowSqlThresholdMs 等）；</li>
 *   <li>初始化上报器（本地队列 + flush + 退避重试）与采样器；</li>
 *   <li>启动 JMX 定时采集；</li>
 *   <li>注册 ByteBuddy AgentBuilder 完成字节码增强（方法埋点 + JDBC 拦截）。</li>
 * </ol>
 *
 * <p>使用示例：
 * <pre>
 *   java -javaagent:perf-agent.jar=reportUrl=http://platform:8080/api/agent/report,appName=demo,sampleRate=1.0 -jar app.jar
 * </pre>
 */
public class AgentPremain {

    /**
     * premain 入口：JVM 启动期挂载。
     *
     * @param agentArgs     形如 "k=v,k=v" 的配置参数
     * @param instrumentation JVM 插桩 API
     */
    public static void premain(String agentArgs, Instrumentation instrumentation) {
        start(agentArgs, instrumentation);
    }

    /**
     * agentmain 入口：运行时 attach（预留，配合 byte-buddy-agent）。
     */
    public static void agentmain(String agentArgs, Instrumentation instrumentation) {
        start(agentArgs, instrumentation);
    }

    private static void start(String agentArgs, Instrumentation instrumentation) {
        AgentConfig config = AgentConfig.fromArgs(agentArgs);
        // targetProjectName 未指定时回退 appName（平台按项目名做准入校验）
        String project = config.targetProjectName.isBlank() ? config.appName : config.targetProjectName;
        config.targetProjectName = project;

        // 1) 初始化上报器与采样器
        HttpReporter http = new HttpReporter();
        MetricsReporter reporter = new MetricsReporter(config, http);
        TraceSampler sampler = new TraceSampler(config.sampleRate);
        TraceContext.init(config.appName, sampler, reporter);
        JdbcInterceptor.init(config, reporter);
        reporter.start();

        // 2) 启动 JMX 采集（独立线程）
        JvmCollector jvmCollector = null;
        if (config.jmxEnabled) {
            jvmCollector = new JvmCollector(config, reporter);
            jvmCollector.start();
        }

        // 3) 注册增强规则（方法埋点 + JDBC 拦截）
        if (config.methodTraceEnabled || config.jdbcEnabled) {
            TraceEnhancer.builder(config)
                    .installOn(instrumentation);
        }

        // 4) 优雅停止：JVM 退出时冲刷剩余数据
        Runtime.getRuntime().addShutdownHook(new Thread(reporter::stop, "perf-agent-shutdown"));
        System.out.println("[perf-agent] 启动完成 appName=" + config.appName
                + " targetProjectName=" + config.targetProjectName
                + " reportUrl=" + config.reportUrl
                + " sampleRate=" + config.sampleRate
                + " jmxCollector=" + (jvmCollector != null));
    }
}
