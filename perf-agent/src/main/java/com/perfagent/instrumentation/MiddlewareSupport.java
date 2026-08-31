package com.perfagent.instrumentation;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.MetricsReporter;
import com.perfagent.trace.TraceContext;

import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.Collection;
import java.util.List;

/**
 * 中间件（Redis / ES / MQ）拦截公共逻辑：耗时计算、命令文本渲染、超阈值上报。
 *
 * <p>与设计上 JDBC 拦截（{@link JdbcInterceptor}）保持一致的低开销策略：
 * <ul>
 *   <li>enter 阶段仅记录 {@code System.nanoTime()} 与参数引用（零字符串操作）；</li>
 *   <li>exit 阶段先判阈值，<b>未超阈值立即返回</b>，不做任何解析；</li>
 *   <li>超阈值才做命令文本渲染、方法链快照与 traceId 关联（慢路径开销可接受）。</li>
 * </ul>
 *
 * <p>本类方法被 Advice 内联代码调用，因此全部为 {@code public static}，
 * 且内部对渲染逻辑全量兜底——任何异常都不得影响被测业务。
 */
public final class MiddlewareSupport {

    /** 中间件类型（落库 slow_command.type，与平台枚举一致） */
    public static final String TYPE_REDIS = "redis";
    public static final String TYPE_ES = "es";
    public static final String TYPE_MQ = "mq";

    /** 参与渲染的最大参数个数（超出记 "+n more"） */
    private static final int MAX_ARGS = 6;
    /** 单个参数渲染最大字符数 */
    private static final int MAX_ARG_CHARS = 200;
    /** 命令文本总长上限（落库前截断） */
    private static final int MAX_COMMAND_CHARS = 1000;
    /** byte[] 参数最大解码字节数（MQ 消息体可能很大） */
    private static final int MAX_BYTES_DECODE = 256;
    /** 容器类参数（数组/集合）递归渲染的最大元素数 */
    private static final int MAX_ELEMENTS = 4;

    /** 上报器引用（AgentPremain 初始化时注入） */
    static volatile MetricsReporter reporter;
    /** 应用名称（AgentPremain 初始化时注入） */
    static volatile String appName = "unknown-app";
    /** 慢命令阈值 ms（AgentPremain 初始化时注入） */
    static volatile long slowCommandThresholdMs = 100;

    private MiddlewareSupport() {
    }

    /** enter 阶段快照：开始时间 + 参数引用（未超阈值时不做任何解析） */
    public static final class Enter {
        final long startNanos;
        final Object[] args;

        public Enter(long startNanos, Object[] args) {
            this.startNanos = startNanos;
            this.args = args;
        }
    }

    /** 初始化（由 AgentPremain 调用） */
    public static void init(AgentConfig config, MetricsReporter metricsReporter) {
        reporter = metricsReporter;
        appName = config.appName;
        slowCommandThresholdMs = config.slowCommandThresholdMs;
    }

    /** enter：仅取时间 + 参数引用 */
    public static Object enter(@SuppressWarnings("unused") long nowNanos, Object[] args) {
        return new Enter(nowNanos, args);
    }

    /**
     * exit：阈值判定 + 命令渲染 + 上报（超阈值才执行重操作）。
     *
     * @param type    中间件类型（redis/es/mq）
     * @param enterObj {@link #enter} 的返回值
     * @param method  被拦截方法（提供命令名，如 GET / basicPublish）
     */
    public static void report(String type, Object enterObj, Method method) {
        try {
            if (!(enterObj instanceof Enter e) || reporter == null) {
                return;
            }
            long costMs = (System.nanoTime() - e.startNanos) / 1_000_000;
            if (costMs < slowCommandThresholdMs) {
                return;
            }
            reporter.reportSlowCommand(appName, type, describe(method, e.args), costMs,
                    System.currentTimeMillis(), TraceContext.currentMethodChain(),
                    TraceContext.currentTraceId());
        } catch (Throwable ignored) {
            // 兜底：观测逻辑绝不因渲染/取链路异常影响业务
        }
    }

    /**
     * 渲染命令文本：方法名 + 参数（各类型可读化，超长截断）。
     * 如 {@code get user:123}、{@code basicPublish order.exchange order.created <byte[42]>}。
     */
    public static String describe(Method method, Object[] args) {
        StringBuilder sb = new StringBuilder(64);
        sb.append(method == null ? "<unknown>" : method.getName());
        if (args != null) {
            int n = Math.min(args.length, MAX_ARGS);
            for (int i = 0; i < n; i++) {
                sb.append(' ');
                sb.append(render(args[i]));
            }
            if (args.length > MAX_ARGS) {
                sb.append(" (+").append(args.length - MAX_ARGS).append(" more)");
            }
        }
        return truncate(sb.toString());
    }

    /** 单参数渲染（全类型兜底，绝不抛异常） */
    static String render(Object v) {
        if (v == null) {
            return "null";
        }
        try {
            if (v instanceof String s) {
                return truncateArg(s);
            }
            if (v instanceof byte[] b) {
                return renderBytes(b);
            }
            if (v instanceof Number || v instanceof Boolean || v instanceof Character) {
                return String.valueOf(v);
            }
            if (v instanceof Object[] arr) {
                return renderContainer(arr.length, i -> arr[i], "[", "]");
            }
            if (v instanceof Collection<?> col) {
                Object[] arr = col.toArray();
                return renderContainer(arr.length, i -> arr[i], "[", "]");
            }
            if (v instanceof Enum<?> e) {
                return e.name();
            }
            // 其余（如 ES Request 对象、RabbitMQ AMQP.BasicProperties）：仅记类型，避免 toString 触发大对象序列化
            return "<" + v.getClass().getSimpleName() + ">";
        } catch (Throwable t) {
            return "<" + v.getClass().getSimpleName() + ">";
        }
    }

    private static String renderContainer(int size, java.util.function.IntFunction<Object> getter,
                                          String open, String close) {
        StringBuilder sb = new StringBuilder(open);
        int n = Math.min(size, MAX_ELEMENTS);
        for (int i = 0; i < n; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(truncateArg(render(getter.apply(i))));
        }
        if (size > MAX_ELEMENTS) {
            sb.append(",…+").append(size - MAX_ELEMENTS);
        }
        return sb.append(close).toString();
    }

    /** byte[] 渲染：UTF-8 解码前 N 字节（MQ 消息体通常很大，仅取头部） */
    static String renderBytes(byte[] b) {
        int n = Math.min(b.length, MAX_BYTES_DECODE);
        String s = new String(b, 0, n, StandardCharsets.UTF_8);
        return "<" + b.length + "B> " + truncateArg(s);
    }

    private static String truncateArg(String s) {
        if (s == null) {
            return "null";
        }
        return s.length() > MAX_ARG_CHARS ? s.substring(0, MAX_ARG_CHARS) + "…" : s;
    }

    /** 命令文本总截断 */
    static String truncate(String s) {
        return s.length() > MAX_COMMAND_CHARS ? s.substring(0, MAX_COMMAND_CHARS) + "..." : s;
    }
}
