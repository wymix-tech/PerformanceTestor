package com.perfagent;

import com.perfagent.config.AgentConfig;
import com.perfagent.hotspot.HotspotSampler;
import com.perfagent.instrumentation.JdbcInterceptor;
import com.perfagent.instrumentation.MiddlewareSupport;
import com.perfagent.instrumentation.TraceEnhancer;
import com.perfagent.jmx.JvmCollector;
import com.perfagent.reporter.HttpReporter;
import com.perfagent.reporter.MetricsReporter;
import com.perfagent.trace.TraceContext;
import com.perfagent.trace.TraceSampler;

import java.lang.instrument.Instrumentation;
import java.util.concurrent.atomic.AtomicBoolean;

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

    /** 启动幂等保护：premain 与 agentmain 双触发时仅初始化一次，防双份采集线程与重复数据 */
    private static final AtomicBoolean STARTED = new AtomicBoolean(false);

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
     * 与 premain 双触发时由 {@link #STARTED} 幂等保护，仅首次生效。
     */
    public static void agentmain(String agentArgs, Instrumentation instrumentation) {
        start(agentArgs, instrumentation);
    }

    private static void start(String agentArgs, Instrumentation instrumentation) {
        if (!STARTED.compareAndSet(false, true)) {
            System.out.println("[perf-agent] 检测到重复启动（premain/attach 双触发），跳过重复初始化");
            return;
        }
        AgentConfig config = AgentConfig.fromArgs(agentArgs);
        // targetProjectName 未指定时回退 appName（平台按项目名做准入校验）
        String project = config.targetProjectName.isBlank() ? config.appName : config.targetProjectName;
        config.targetProjectName = project;

        // 1) 初始化上报器与采样器（采样率控制比例 + 每秒令牌桶控制绝对量）
        // 探针令牌由启动参数 agentToken 下发，未配置时不携带请求头（兼容平台未启用令牌校验）
        HttpReporter http = new HttpReporter(config.agentToken);
        MetricsReporter reporter = new MetricsReporter(config, http);
        TraceSampler sampler = new TraceSampler(config.sampleRate, config.maxTracesPerSec);
        TraceContext.init(config.appName, sampler, reporter);
        JdbcInterceptor.init(config, reporter);
        MiddlewareSupport.init(config, reporter);
        reporter.start();

        // 2) 启动 JMX 采集（独立线程）
        JvmCollector jvmCollector = null;
        if (config.jmxEnabled) {
            jvmCollector = new JvmCollector(config, reporter);
            jvmCollector.start();
        }

        // 2.1) CPU 热点弱采样（默认关闭；开启后回答"CPU 烧在哪"）
        HotspotSampler hotspotSampler = null;
        if (config.hotspotEnabled) {
            hotspotSampler = new HotspotSampler(config, reporter);
            hotspotSampler.start();
        }

        // 3) 注册增强规则（方法埋点 + JDBC 拦截 + 中间件拦截）
        if (config.methodTraceEnabled || config.jdbcEnabled || config.middlewareEnabled) {
            TraceEnhancer.builder(config)
                    .installOn(instrumentation);
        }

        // 4) 优雅停止：JVM 退出时停采集器并冲刷剩余数据
        final JvmCollector collector = jvmCollector;
        final HotspotSampler hotspot = hotspotSampler;
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            if (collector != null) {
                collector.stop();
            }
            if (hotspot != null) {
                hotspot.stop();
            }
            reporter.stop();
        }, "perf-agent-shutdown"));
        System.out.println("[perf-agent] 启动完成 appName=" + config.appName
                + " targetProjectName=" + config.targetProjectName
                + " reportUrl=" + config.reportUrl
                + " sampleRate=" + config.sampleRate
                + " jmxCollector=" + (jvmCollector != null)
                + " hotspotSampler=" + (hotspotSampler != null));
    }
}
