package com.perfagent.instrumentation;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.MetricsReporter;
import com.perfagent.trace.TraceContext;
import net.bytebuddy.asm.Advice;

/**
 * JDBC 执行拦截 Advice：拦截 Statement / PreparedStatement / CallableStatement
 * 的 execute / executeQuery / executeUpdate / executeBatch 方法，记录 SQL 文本与执行耗时，
 * 超过阈值（默认 500ms）标记为慢 SQL 上报平台（JDBC 拦截通道，与 ORM 无关）。
 *
 * <p>SQL 文本提取策略：
 * <ul>
 *   <li>带 String 参数的重载（Statement.execute(sql) 等）：直接取第一个参数；</li>
 *   <li>无参重载（PreparedStatement.execute() 等）：尝试通过 toString() 提取驱动返回的 SQL。</li>
 * </ul>
 */
public class JdbcInterceptor {

    /** OnMethodEnter 返回值：SQL 参数引用（可能为 null）与开始时间（statement 供退出时反查连接 URL） */
    public static class JdbcEnter {
        public final String sql;
        public final long startNanos;
        public final Object statement;

        public JdbcEnter(String sql, long startNanos, Object statement) {
            this.sql = sql;
            this.startNanos = startNanos;
            this.statement = statement;
        }
    }

    /** 慢 SQL 阈值（AgentPremain 初始化时注入） */
    public static volatile long slowSqlThresholdMs = 500;
    /** 上报器引用（AgentPremain 初始化时注入） */
    public static volatile MetricsReporter reporter;
    /** 应用名称（AgentPremain 初始化时注入） */
    public static volatile String appName = "unknown-app";

    /** 初始化（由 AgentPremain 调用） */
    public static void init(AgentConfig config, MetricsReporter metricsReporter) {
        slowSqlThresholdMs = config.slowSqlThresholdMs;
        reporter = metricsReporter;
        appName = config.appName;
    }

    /**
     * 惰性 SQL 提取（enter 阶段）：仅取第一个 String 参数的<b>引用</b>（零拷贝、零截断、零 toString）。
     * 绝大多数 SQL 未超阈值即被丢弃——此时除了这一次 instanceof 扫描外不做任何字符串操作。
     */
    @Advice.OnMethodEnter
    public static Object enter(@Advice.AllArguments Object[] args,
                               @Advice.This Object self) {
        String sqlArg = null;
        if (args != null) {
            for (Object arg : args) {
                if (arg instanceof String s && !s.isBlank()) {
                    sqlArg = s;
                    break;
                }
            }
        }
        return new JdbcEnter(sqlArg, System.nanoTime(), self);
    }

    @Advice.OnMethodExit(onThrowable = Throwable.class)
    public static void exit(@Advice.Enter Object enter) {
        if (enter == null) {
            return;
        }
        JdbcEnter je = (JdbcEnter) enter;
        long costMs = (System.nanoTime() - je.startNanos) / 1_000_000;
        if (costMs >= slowSqlThresholdMs && reporter != null) {
            // 超阈值才做完整 SQL 解析（toString 兜底 + 截断）与方法链快照
            reporter.reportSlowSql(appName, resolveSql(je), costMs, System.currentTimeMillis(),
                    TraceContext.currentMethodChain(), extractDbHost(je.statement),
                    TraceContext.currentStressTaskId());
        }
    }

    /**
     * 从 Statement 反查 JDBC 连接 URL，提取 host:port/db 数据库标识。
     * 必须为 public（被 Advice 内联代码调用），内部全量兜底，绝不抛出异常影响业务。
     */
    public static String extractDbHost(Object statement) {
        try {
            if (statement instanceof java.sql.Statement st) {
                String url = st.getConnection().getMetaData().getURL();
                // 形如 jdbc:mysql://host:3306/db?params → host:3306/db
                if (url != null) {
                    int idx = url.indexOf("://");
                    if (idx > 0) {
                        String rest = url.substring(idx + 3);
                        int q = rest.indexOf('?');
                        return q > 0 ? rest.substring(0, q) : rest;
                    }
                }
            }
        } catch (Throwable ignored) {
            // 驱动代理或连接已关闭等场景，忽略
        }
        return "";
    }

    /**
     * 解析完整 SQL（exit 阶段、超阈值后才调用）：有 String 参数直接截断；
     * 否则尝试从驱动对象的 toString() 提取（PreparedStatement 无参 execute）。
     * <p>注意：该方法被 Advice 内联代码引用（exit 方法体会被复制到目标类），
     * 必须为 public 以免内联后跨类访问私有方法触发 IllegalAccessError。
     */
    public static String resolveSql(JdbcEnter je) {
        if (je.sql != null) {
            return truncate(je.sql);
        }
        if (je.statement != null) {
            try {
                String s = je.statement.toString();
                if (s != null && !s.isBlank()) {
                    // 很多驱动 toString 形如 "com.mysql...StatementImpl[id=1, ...]sql=SELECT ..."
                    int idx = s.toLowerCase().indexOf("sql");
                    String candidate = idx >= 0 ? s.substring(idx) : s;
                    return truncate("<prepared> " + candidate);
                }
            } catch (Throwable ignored) {
                // 忽略 toString 异常，不影响业务
            }
        }
        return "<unknown-sql>";
    }

    /** 截断超长 SQL（public：被内联代码调用，见 resolveSql 说明） */
    public static String truncate(String sql) {
        return sql.length() > 2000 ? sql.substring(0, 2000) + "..." : sql;
    }
}
