package com.perfagent.instrumentation;

import com.perfagent.config.AgentConfig;
import com.perfagent.util.AppManifest;
import net.bytebuddy.agent.builder.AgentBuilder;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.description.method.MethodDescription;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.matcher.ElementMatcher;

import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;

import static net.bytebuddy.matcher.ElementMatchers.hasSuperType;
import static net.bytebuddy.matcher.ElementMatchers.isAbstract;
import static net.bytebuddy.matcher.ElementMatchers.isAnnotatedWith;
import static net.bytebuddy.matcher.ElementMatchers.isBridge;
import static net.bytebuddy.matcher.ElementMatchers.isInterface;
import static net.bytebuddy.matcher.ElementMatchers.isStatic;
import static net.bytebuddy.matcher.ElementMatchers.isSubTypeOf;
import static net.bytebuddy.matcher.ElementMatchers.isSynthetic;
import static net.bytebuddy.matcher.ElementMatchers.nameStartsWith;
import static net.bytebuddy.matcher.ElementMatchers.named;
import static net.bytebuddy.matcher.ElementMatchers.not;

/**
 * 增强规则定义：配置 ByteBuddy 拦截哪些类与方法。
 *
 * <p><b>业务方法埋点（业务包全量）</b>——只埋业务代码，天然排除 JDK 与第三方依赖：
 * <ol>
 *   <li><b>显式配置</b>：{@code includePackages=com.acme.core,com.acme.mall}，覆盖业务包内
 *       <b>全部类</b>（Controller/Service/Repository/Component/工具类/静态方法，不要求 Spring 注解）；</li>
 *   <li><b>自动推导（推荐，零配置）</b>：读取被测应用 fat jar 的 Start-Class/Main-Class，
 *       其包名 = 业务根包。如 com.perf.demo.PerfDemoApplication → com.perf.demo 包下全部类；</li>
 *   <li><b>注解兜底</b>：推导失败（IDE exploded 运行等）时回退 Spring 注解匹配
 *       （Controller/RestController/Service/Repository/Component）。</li>
 * </ol>
 *
 * <p><b>POJO / 数据类不埋点</b>（三层判断，任一命中即跳过）：
 * <ul>
 *   <li>包名启发：entity/entities/model/models/dto/vo/po/bean/beans/domain 包下的类；</li>
 *   <li>结构判断：除构造器/静态方法外全部方法都是 getter/setter/equals/hashCode 等数据风格 → 纯数据类；</li>
 *   <li>枚举与 record。</li>
 * </ul>
 * 方法级噪音过滤另见 {@link MethodFilter}（构造器/Object 方法/getter/setter/桥接合成/main）。
 *
 * <p><b>JDBC 拦截</b>：java.sql.Statement 全部实现类（含 PreparedStatement/CallableStatement）。
 */
public class TraceEnhancer {

    /** POJO 常见包段（包名启发式排除，避免实体/DTO 类被埋） */
    private static final List<String> POJO_PKG_SEGMENTS = List.of(
            "entity", "entities", "model", "models", "dto", "vo", "po",
            "bean", "beans", "domain");

    private TraceEnhancer() {
    }

    /** 构建 AgentBuilder：方法埋点 + JDBC 拦截 */
    public static AgentBuilder builder(AgentConfig config) {
        AgentBuilder agentBuilder = new AgentBuilder.Default()
                .disableClassFormatChanges()
                .with(AgentBuilder.RedefinitionStrategy.RETRANSFORMATION)
                .with(AgentBuilder.Listener.StreamWriting.toSystemError().withErrorsOnly());

        // 1) 业务方法埋点（业务包全量 / 注解兜底；POJO 数据类跳过）
        if (config.methodTraceEnabled) {
            agentBuilder = agentBuilder
                    .type(businessTypeMatcher(config))
                    .transform(TraceEnhancer::transformMethodAdvice);
        }

        // 1.1) Servlet Filter 根拦截：以最外层 Filter#doFilter 作为唯一采样决策点，
        //      修复"Controller 未采样时下游方法重复成为根"导致的采样稀释与半截链路。
        //      按类型名匹配（jakarta/javax 双协议，hasSuperType 走名称匹配无需加载 Servlet 类）。
        if (config.methodTraceEnabled && config.filterRootEnabled) {
            agentBuilder = agentBuilder
                    .type(hasSuperType(named("jakarta.servlet.Filter"))
                            .or(hasSuperType(named("javax.servlet.Filter")))
                            .and(not(isInterface()))
                            .and(not(isAbstract()))
                            .and(not(nameStartsWith("com.perfagent.")))
                            .and(not(nameStartsWith("net.bytebuddy."))))
                    .transform((builder, typeDescription, classLoader, module, protectionDomain) ->
                            builder.visit(Advice.to(FilterRootAdvice.class)
                                    .withExceptionHandler(Advice.ExceptionHandler.Default.PRINTING)
                                    .on(named("doFilter").and(not(isStatic())))));
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
                            builder.visit(Advice.to(JdbcInterceptor.class)
                                    .withExceptionHandler(Advice.ExceptionHandler.Default.PRINTING)
                                    .on(named("execute").or(named("executeQuery"))
                                            .or(named("executeUpdate")).or(named("executeBatch"))
                                            .and(not(isStatic())))));
        }
        // 3) 中间件拦截（Redis / ES / MQ）：默认关闭，需 middlewareEnabled=true 显式开启
        if (config.middlewareEnabled) {
            agentBuilder = agentBuilder
                    .type(redisTypeMatcher())
                    .transform(middlewareTransform(RedisInterceptor.class));
            agentBuilder = agentBuilder
                    .type(esTypeMatcher())
                    .transform((builder, typeDescription, classLoader, module, protectionDomain) ->
                            builder.visit(Advice.to(EsInterceptor.class)
                                    .withExceptionHandler(Advice.ExceptionHandler.Default.PRINTING)
                                    // 只拦同步 performRequest：performRequestAsync 返回 Future，
                                    // OnMethodExit 时请求尚未真正执行，测到的是"提交耗时"（约 0ms），
                                    // 慢 ES 查询会全部漏报。覆盖异步路径需在 Future 完成回调处计时，
                                    // 而包装返回值要求 agent 编译期依赖 ES 客户端类型（Cancellable），
                                    // 与"agent 零业务依赖"的约束冲突，故本期明确只覆盖同步路径。
                                    .on(named("performRequest").and(not(isStatic())))));
            agentBuilder = agentBuilder
                    .type(mqTypeMatcher())
                    .transform((builder, typeDescription, classLoader, module, protectionDomain) ->
                            builder.visit(Advice.to(MqInterceptor.class)
                                    .withExceptionHandler(Advice.ExceptionHandler.Default.PRINTING)
                                    .on(mqMethodMatcher().and(not(isStatic())))));
        }
        return agentBuilder;
    }

    /**
     * Redis 类型匹配：Lettuce 同步命令接口 + Jedis 命令接口（按类型名匹配，
     * 无需在 agent 侧引入任何 Redis 依赖，类不存在时匹配器自然返回 false）。
     */
    private static ElementMatcher.Junction<TypeDescription> redisTypeMatcher() {
        return hasSuperType(named("io.lettuce.core.api.sync.RedisCommands"))
                .or(hasSuperType(named("io.lettuce.core.api.sync.BaseRedisCommands")))
                .or(hasSuperType(named("redis.clients.jedis.Jedis")))
                .or(hasSuperType(named("redis.clients.jedis.commands.JedisCommands")))
                .and(not(isInterface()))
                .and(not(isAbstract()))
                .and(not(nameStartsWith("com.perfagent.")))
                .and(not(nameStartsWith("net.bytebuddy.")));
    }

    /**
     * ES 类型匹配：RestClient 子类。RestHighLevelClient 的 search/index/bulk
     * 最终都经由 RestClient#performRequest 发出，故只拦底层避免重复计数。
     */
    private static ElementMatcher.Junction<TypeDescription> esTypeMatcher() {
        // hasSuperType 不含类型自身，RestClient 为具体类需显式 or 自身
        return named("org.elasticsearch.client.RestClient")
                .or(hasSuperType(named("org.elasticsearch.client.RestClient")))
                .and(not(isInterface()))
                .and(not(isAbstract()))
                .and(not(nameStartsWith("com.perfagent.")))
                .and(not(nameStartsWith("net.bytebuddy.")));
    }

    /** MQ 类型匹配：RabbitMQ Channel 实现类 */
    private static ElementMatcher.Junction<TypeDescription> mqTypeMatcher() {
        return hasSuperType(named("com.rabbitmq.client.Channel"))
                .and(not(isInterface()))
                .and(not(isAbstract()))
                .and(not(nameStartsWith("com.perfagent.")))
                .and(not(nameStartsWith("net.bytebuddy.")));
    }

    /** RabbitMQ 关注的命令方法（发布/消费/确认/声明/事务） */
    private static ElementMatcher.Junction<MethodDescription> mqMethodMatcher() {
        return named("basicPublish").or(named("basicGet")).or(named("basicConsume"))
                .or(named("basicAck")).or(named("basicNack")).or(named("basicReject"))
                .or(named("queueDeclare")).or(named("queueBind")).or(named("exchangeDeclare"))
                .or(named("txCommit")).or(named("txRollback"));
    }

    /**
     * 中间件方法匹配（Lettuce/Jedis 全命令方法）：排除静态、抽象、桥接合成、
     * 生命周期与 Object 方法，避免噪音与重复计数。
     *
     * <p>Lettuce 的 {@code BaseRedisCommands} 继承链上混有连接管理方法
     * （{@code isOpen/reset/setTimeout/flushCommands/setAutoFlushCommands/size} 等），
     * 非真实数据命令，白名单式排除列表拦不全时会产生埋点噪音，故一并排除。
     */
    private static ElementMatcher.Junction<MethodDescription> middlewareMethods() {
        return not(isStatic())
                .and(not(isAbstract()))
                .and(not(isSynthetic()))
                .and(not(isBridge()))
                .and(not(named("close")))
                .and(not(named("shutdown")))
                .and(not(named("toString")))
                .and(not(named("hashCode")))
                .and(not(named("equals")))
                .and(not(named("isOpen")))
                .and(not(named("reset")))
                .and(not(named("size")))
                .and(not(nameStartsWith("setTimeout")))
                .and(not(nameStartsWith("setAutoFlushCommands")))
                .and(not(nameStartsWith("flushCommands")))
                .and(not(nameStartsWith("activeCommandsCount")))
                .and(not(nameStartsWith("dispatch")))
                .and(not(nameStartsWith("lambda$")));
    }

    /** 中间件 transform（复用统一方法过滤） */
    private static AgentBuilder.Transformer middlewareTransform(Class<?> advice) {
        return (builder, typeDescription, classLoader, module, protectionDomain) ->
                builder.visit(Advice.to(advice)
                        .withExceptionHandler(Advice.ExceptionHandler.Default.PRINTING)
                        .on(middlewareMethods()));
    }

    /** 方法埋点 transform：POJO/数据类直接返回原 builder（不增强） */
    private static net.bytebuddy.dynamic.DynamicType.Builder<?> transformMethodAdvice(
            net.bytebuddy.dynamic.DynamicType.Builder<?> builder,
            TypeDescription typeDescription,
            ClassLoader classLoader,
            net.bytebuddy.utility.JavaModule module,
            java.security.ProtectionDomain protectionDomain) {
        if (isPojo(typeDescription)) {
            return builder;
        }
        return builder.visit(Advice.to(MethodTimerAdvice.class)
                .withExceptionHandler(Advice.ExceptionHandler.Default.PRINTING)
                .on(MethodFilter.matcher()));
    }

    /**
     * POJO / 数据类判断（三层任一命中即不埋）：
     * <ol>
     *   <li>包名启发（entity/dto/vo/model 等包段）；</li>
     *   <li>枚举 / record（java.lang.Record 标记接口）；</li>
     *   <li>结构判断：非静态实例方法全部为 getter/setter/Object/桥接合成 → 纯数据类。</li>
     * </ol>
     */
    static boolean isPojo(TypeDescription type) {
        if (type.isEnum() || type.isRecord()) {
            return true;
        }
        String name = type.getActualName();
        for (String seg : POJO_PKG_SEGMENTS) {
            if (name.contains("." + seg + ".")) {
                return true;
            }
        }
        // 结构判断：除构造器/类型初始化外的<b>全部</b>方法（含静态）均为数据风格 → 纯数据类；
        // 静态业务方法（工具类）参与判断，防止"静态工具 + 实例 getter"混合类被误判
        for (MethodDescription m : type.getDeclaredMethods()) {
            if (m.isConstructor() || m.isTypeInitializer()) {
                continue;
            }
            if (!MethodFilter.isPojoStyleMethod(m)) {
                return false;
            }
        }
        return true;
    }

    /**
     * 业务类型匹配器：显式包前缀 > Start-Class 包推导 > Spring 注解兜底。
     */
    private static ElementMatcher.Junction<TypeDescription> businessTypeMatcher(AgentConfig config) {
        List<String> pkgs = resolveBusinessPackages(config);
        if (!pkgs.isEmpty()) {
            System.out.println("[perf-agent] 埋点范围（业务包全量）: " + pkgs);
            ElementMatcher.Junction<TypeDescription> match = nameStartsWith(pkgs.get(0));
            for (int i = 1; i < pkgs.size(); i++) {
                match = match.or(nameStartsWith(pkgs.get(i)));
            }
            return match
                    .and(not(isInterface()))
                    .and(not(isAbstract()));
        }
        System.out.println("[perf-agent] 埋点范围：业务包推导失败，回退 Spring 注解兜底策略");
        return isAnnotatedWith(named("org.springframework.stereotype.Controller"))
                .or(isAnnotatedWith(named("org.springframework.web.bind.annotation.RestController")))
                .or(isAnnotatedWith(named("org.springframework.stereotype.Service")))
                .or(isAnnotatedWith(named("org.springframework.stereotype.Repository")))
                .or(isAnnotatedWith(named("org.springframework.stereotype.Component")))
                .and(not(isInterface()))
                .and(not(isAbstract()))
                .and(not(nameStartsWith("org.springframework.")));
    }

    /**
     * 解析业务根包：显式配置优先；否则从被测 jar 的 Start-Class/Main-Class 包名推导。
     *
     * @return 业务包前缀列表；无法推导时返回空列表（触发注解兜底策略）
     */
    private static List<String> resolveBusinessPackages(AgentConfig config) {
        // 1) 显式配置
        if (config.includePackages != null && !config.includePackages.isBlank()) {
            List<String> pkgs = new ArrayList<>();
            for (String p : config.includePackages.split(",")) {
                if (!p.isBlank()) {
                    pkgs.add(p.trim());
                }
            }
            return pkgs;
        }
        // 2) Start-Class / Main-Class 包推导（业务根包）
        String startClass = AppManifest.startClass();
        if (startClass != null && startClass.contains(".")) {
            String rootPkg = startClass.substring(0, startClass.lastIndexOf('.'));
            // 单级根包（如 "com"）易误伤同前缀第三方库，视为推导失败走兜底
            if (!rootPkg.contains(".")) {
                return List.of();
            }
            return List.of(rootPkg);
        }
        return List.of();
    }
}
