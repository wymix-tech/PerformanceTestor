package com.perfagent.instrumentation;

import com.perfagent.config.AgentConfig;
import com.perfagent.reporter.MetricsReporter;
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

    /** OnMethodEnter 返回值：SQL 文本与开始时间 */
    public static class JdbcEnter {
        public final String sql;
        public final long startNanos;

        public JdbcEnter(String sql, long startNanos) {
            this.sql = sql;
            this.startNanos = startNanos;
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

    @Advice.OnMethodEnter
    public static Object enter(@Advice.Origin("#m") String methodName,
                               @Advice.AllArguments Object[] args,
                               @Advice.This Object self) {
        return new JdbcEnter(extractSql(methodName, args, self), System.nanoTime());
    }

    @Advice.OnMethodExit(onThrowable = Throwable.class)
    public static void exit(@Advice.Enter Object enter) {
        if (enter == null) {
            return;
        }
        JdbcEnter je = (JdbcEnter) enter;
        long costMs = (System.nanoTime() - je.startNanos) / 1_000_000;
        if (costMs >= slowSqlThresholdMs && reporter != null) {
            reporter.reportSlowSql(appName, je.sql, costMs, System.currentTimeMillis());
        }
    }

    /** 提取 SQL 文本：优先取 String 参数，否则尝试 toString() */
    private static String extractSql(String methodName, Object[] args, Object self) {
        if (args != null) {
            for (Object arg : args) {
                if (arg instanceof String sql && !sql.isBlank()) {
                    return truncate(sql);
                }
            }
        }
        // PreparedStatement 无参 execute：尝试从驱动对象的 toString() 提取 SQL
        if (self != null) {
            try {
                String s = self.toString();
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

    private static String truncate(String sql) {
        return sql.length() > 2000 ? sql.substring(0, 2000) + "..." : sql;
    }
}
