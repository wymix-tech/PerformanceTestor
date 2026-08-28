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

    /** OnMethodEnter 返回值：SQL 文本与开始时间（statement 供退出时反查连接 URL） */
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

    @Advice.OnMethodEnter
    public static Object enter(@Advice.Origin("#m") String methodName,
                               @Advice.AllArguments Object[] args,
                               @Advice.This Object self) {
        return new JdbcEnter(extractSql(methodName, args, self), System.nanoTime(), self);
    }

    @Advice.OnMethodExit(onThrowable = Throwable.class)
    public static void exit(@Advice.Enter Object enter) {
        if (enter == null) {
            return;
        }
        JdbcEnter je = (JdbcEnter) enter;
        long costMs = (System.nanoTime() - je.startNanos) / 1_000_000;
        if (costMs >= slowSqlThresholdMs && reporter != null) {
            // 记录触发该 SQL 的调用方法链与目标数据库标识，便于平台侧精确定位源码与执行 EXPLAIN
            reporter.reportSlowSql(appName, je.sql, costMs, System.currentTimeMillis(),
                    TraceContext.currentMethodChain(), extractDbHost(je.statement));
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
     * 提取 SQL 文本：优先取 String 参数，否则尝试 toString()。
     * <p>注意：该方法被 Advice 内联代码引用（enter 方法体会被复制到目标类，
     * 如 HikariProxyStatement），必须为 public 以免内联后跨类访问私有方法
     * 触发 IllegalAccessError。
     */
    public static String extractSql(String methodName, Object[] args, Object self) {
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

    /** 截断超长 SQL（public：被内联代码调用，见 extractSql 说明） */
    public static String truncate(String sql) {
        return sql.length() > 2000 ? sql.substring(0, 2000) + "..." : sql;
    }
}
