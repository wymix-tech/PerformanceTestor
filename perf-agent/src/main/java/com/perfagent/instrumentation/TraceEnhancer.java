package com.perfagent.instrumentation;

import com.perfagent.config.AgentConfig;
import net.bytebuddy.agent.builder.AgentBuilder;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.matcher.ElementMatcher;

import java.sql.Statement;

import static net.bytebuddy.matcher.ElementMatchers.*;

/**
 * 增强规则定义：配置 ByteBuddy 拦截哪些类与方法。
 *
 * <p>业务方法埋点：拦截标注 Spring 分层注解（Controller/Service/Repository）的类，
 * 排除 perf-agent 自身包、JDK 与第三方框架内部类，避免递归增强。
 *
 * <p>JDBC 拦截：拦截 java.sql.Statement 的所有实现类（含 PreparedStatement/CallableStatement）。
 */
public class TraceEnhancer {

    private TraceEnhancer() {
    }

    /** 构建 AgentBuilder：方法埋点 + JDBC 拦截 */
    public static AgentBuilder builder(AgentConfig config) {
        AgentBuilder agentBuilder = new AgentBuilder.Default()
                .disableClassFormatChanges()
                .with(AgentBuilder.RedefinitionStrategy.RETRANSFORMATION)
                .with(AgentBuilder.Listener.StreamWriting.toSystemError().withErrorsOnly());

        // 1) 业务方法埋点（Spring 分层注解 + 用户包名兜底）
        if (config.methodTraceEnabled) {
            agentBuilder = agentBuilder
                    .type(isAnnotatedWithAny(
                                    named("org.springframework.stereotype.Controller"),
                                    named("org.springframework.web.bind.annotation.RestController"),
                                    named("org.springframework.stereotype.Service"),
                                    named("org.springframework.stereotype.Repository"))
                            .and(not(isInterface()))
                            .and(not(isAbstract()))
                            .and(not(nameStartsWith("com.perfagent.")))
                            .and(not(nameStartsWith("net.bytebuddy.")))
                            .and(not(nameStartsWith("org.springframework."))))
                    .transform((builder, typeDescription, classLoader, module, protectionDomain) ->
                            builder.visit(net.bytebuddy.asm.Advice.to(MethodTimerAdvice.class)
                                    .withExceptionHandler(net.bytebuddy.asm.Advice.ExceptionHandler.Default.PRINTING)
                                    .on(isMethod().and(not(isConstructor()))
                                            .and(not(isStatic()))
                                            .and(isPublic().or(isProtected())))));
        }

        // 2) JDBC 执行拦截
        if (config.jdbcEnabled) {
            agentBuilder = agentBuilder
                    .type(isSubTypeOf(Statement.class)
                            .and(not(isInterface()))
                            .and(not(isAbstract()))
                            .and(not(nameStartsWith("com.perfagent.")))
                            .and(not(nameStartsWith("net.bytebuddy.")))
                            .and(not(nameStartsWith("org.springframework.jdbc.support."))))
                    .transform((builder, typeDescription, classLoader, module, protectionDomain) ->
                            builder.visit(net.bytebuddy.asm.Advice.to(JdbcInterceptor.class)
                                    .withExceptionHandler(net.bytebuddy.asm.Advice.ExceptionHandler.Default.PRINTING)
                                    .on(named("execute").or(named("executeQuery"))
                                            .or(named("executeUpdate")).or(named("executeBatch"))
                                            .and(not(isStatic())))));
        }
        return agentBuilder;
    }

    /** 便捷匹配：类型标注任一注解 */
    private static ElementMatcher.Junction<TypeDescription> isAnnotatedWithAny(ElementMatcher.Junction<TypeDescription>... matchers) {
        ElementMatcher.Junction<TypeDescription> result = none();
        for (ElementMatcher.Junction<TypeDescription> m : matchers) {
            result = result.or(isAnnotatedWith(m));
        }
        return result;
    }
}
