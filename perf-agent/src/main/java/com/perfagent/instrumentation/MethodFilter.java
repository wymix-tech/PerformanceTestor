package com.perfagent.instrumentation;

import net.bytebuddy.description.method.MethodDescription;
import net.bytebuddy.matcher.ElementMatcher;
import net.bytebuddy.matcher.ElementMatchers;

import java.util.List;

import static net.bytebuddy.matcher.ElementMatchers.isBridge;
import static net.bytebuddy.matcher.ElementMatchers.isMethod;
import static net.bytebuddy.matcher.ElementMatchers.isSynthetic;
import static net.bytebuddy.matcher.ElementMatchers.named;
import static net.bytebuddy.matcher.ElementMatchers.not;
import static net.bytebuddy.matcher.ElementMatchers.returns;
import static net.bytebuddy.matcher.ElementMatchers.takesArguments;
import static net.bytebuddy.matcher.ElementMatchers.takesNoArguments;

/**
 * 方法级过滤：业务包全量埋点后防噪音、防误伤。
 *
 * <p>排除规则（命中任一则不埋）：
 * <ul>
 *   <li>构造器（Advice 的 onThrowable 语义不适用于构造器，且无耗时意义）；</li>
 *   <li>{@code main} 方法；</li>
 *   <li>Object 基类方法：toString/hashCode/equals/clone/getClass/wait/notify/notifyAll（吞吐大、无业务价值）；</li>
 *   <li>getter/setter/is/has 判定（POJO 噪音）；</li>
 *   <li>桥接与合成方法（编译器生成）。</li>
 * </ul>
 *
 * <p>注意：static 方法<b>不排除</b>——工具类多为静态方法，属于埋点目标。
 */
public final class MethodFilter {

    private MethodFilter() {
    }

    /** Object 基类方法名清单 */
    private static final ElementMatcher.Junction<MethodDescription> OBJECT_METHODS =
            named("toString").or(named("hashCode")).or(named("equals"))
                    .or(named("clone")).or(named("getClass")).or(named("wait"))
                    .or(named("notify")).or(named("notifyAll"));

    /** getter：get、is、has 前缀、无参、非 void 返回 */
    private static final ElementMatcher.Junction<MethodDescription> GETTERS =
            ElementMatchers.nameStartsWith("get")
                    .or(ElementMatchers.nameStartsWith("is"))
                    .or(ElementMatchers.nameStartsWith("has"))
                    .and(takesNoArguments())
                    .and(not(returns(void.class)));

    /** setter：set* 前缀、单参数、void 返回 */
    private static final ElementMatcher.Junction<MethodDescription> SETTERS =
            ElementMatchers.nameStartsWith("set")
                    .and(takesArguments(1))
                    .and(returns(void.class));

    /** 埋点目标方法匹配器（公共入口，供 TraceEnhancer 引用） */
    public static ElementMatcher.Junction<MethodDescription> matcher() {
        return isMethod()
                .and(not(isBridge()))
                .and(not(isSynthetic()))
                .and(not(named("main")))
                .and(not(OBJECT_METHODS))
                .and(not(GETTERS))
                .and(not(SETTERS));
    }

    /** Object 基类方法名集合（POJO 结构判断复用） */
    static final List<String> OBJECT_NAMES = List.of(
            "toString", "hashCode", "equals", "clone", "getClass",
            "wait", "notify", "notifyAll");

    /**
     * POJO 风格方法判断（供 {@link TraceEnhancer} 的类型级 POJO 识别复用）：
     * getter/setter/is/has/构建器（build 前缀返回对象自身）/Object 基类方法/桥接合成。
     */
    static boolean isPojoStyleMethod(MethodDescription m) {
        String n = m.getName();
        if (m.isBridge() || m.isSynthetic()) {
            return true;
        }
        if (OBJECT_NAMES.contains(n)) {
            return true;
        }
        return n.startsWith("get") || n.startsWith("is") || n.startsWith("has")
                || n.startsWith("set") || n.startsWith("build");
    }
}
