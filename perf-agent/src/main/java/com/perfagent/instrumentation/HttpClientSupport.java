package com.perfagent.instrumentation;

import com.perfagent.trace.TraceContext;

import java.lang.ClassValue;
import java.lang.reflect.Method;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Consumer;

/**
 * HTTP 客户端出口注入支持（P2-2）：在请求发出前反射注入
 * {@code traceparent}（W3C 跨服务串联）与 {@code X-Perf-Stress-Task-Id}（压测染色透传）。
 *
 * <p>设计约束（探针红线）：
 * <ul>
 *   <li>零编译期依赖：全部经反射调用目标客户端 API，方法引用按类+签名缓存
 *       （O(1)），类不存在/方法不可达时静默跳过；</li>
 *   <li>仅在 perf 上下文内注入（traceparent 或染色标识存在），
 *       非压测/无链路流量零改动；</li>
 *   <li>幂等去重：请求已带 {@code traceparent}（应用自身已注入）时跳过，绝不覆盖；</li>
 *   <li>任何异常吞掉（Throwable），绝不影响被测应用请求。</li>
 * </ul>
 */
public final class HttpClientSupport {

    private HttpClientSupport() {
    }

    /** 是否处于 perf 上下文（traceparent 或染色标识存在），避免无关流量反射开销 */
    private static boolean hasPerfContext() {
        return TraceContext.currentTraceparent() != null
                || TraceContext.currentStressTaskId() != null;
    }

    /** 反射方法缓存键：类 + 方法签名（嵌套 ClassValue 二级缓存，热路径 O(1)） */
    private static final ClassValue<ConcurrentHashMap<String, Method>> SIG_CACHE =
            new ClassValue<>() {
                @Override
                protected ConcurrentHashMap<String, Method> computeValue(Class<?> type) {
                    return new ConcurrentHashMap<>();
                }
            };

    private static Method reqMethod(Object target, String name, Class<?>... paramTypes) {
        if (target == null) {
            return null;
        }
        String key = name + "#" + paramTypes.length;
        return SIG_CACHE.get(target.getClass()).computeIfAbsent(key, k -> {
            try {
                Method m = target.getClass().getMethod(name, paramTypes);
                m.setAccessible(true);
                return m;
            } catch (Throwable t) {
                return null;
            }
        });
    }

    /** 携带染色标识时执行注入动作（traceparent 无条件注入由调用方完成） */
    private static void injectStressHeader(Consumer<String> setter) {
        try {
            String task = TraceContext.currentStressTaskId();
            if (task != null && !task.isBlank()) {
                setter.accept(task);
            }
        } catch (Throwable ignored) {
            // 静默
        }
    }

    /**
     * Apache HttpClient 注入：对 {@code HttpUriRequest}（execute 入参）反射
     * {@code getFirstHeader(String)} 判重 + {@code addHeader(String, String)} 注入。
     * 非请求对象（如 execute(HttpHost, ...) 的 HttpHost 入参）反射失败静默跳过。
     */
    public static void injectApache(Object request) {
        try {
            if (request == null || !hasPerfContext()) {
                return;
            }
            Method first = reqMethod(request, "getFirstHeader", String.class);
            if (first == null) {
                return;
            }
            if (first.invoke(request, "traceparent") != null) {
                return; // 应用自身已注入：不覆盖
            }
            Method add = reqMethod(request, "addHeader", String.class, String.class);
            if (add != null) {
                add.invoke(request, "traceparent", TraceContext.currentTraceparent());
                injectStressHeader(v -> {
                    try {
                        add.invoke(request, "X-Perf-Stress-Task-Id", v);
                    } catch (Throwable ignored) {
                        // 静默
                    }
                });
            }
        } catch (Throwable ignored) {
            // 注入失败静默：不影响业务请求
        }
    }

    /**
     * OkHttp / JDK HttpClient 注入：请求不可变，经
     * {@code newBuilder().setHeader(...)/header(...).build()} 重建后返回替换对象。
     * 无 builder API 或注入失败时返回原对象。
     */
    public static Object injectRequest(Object request) {
        try {
            if (request == null || !hasPerfContext()) {
                return request;
            }
            Method newBuilder = reqMethod(request, "newBuilder");
            if (newBuilder == null) {
                return request;
            }
            Object builder = newBuilder.invoke(request);
            Method header = reqMethod(builder, "setHeader", String.class, String.class);
            if (header == null) {
                // OkHttp Request.Builder：header(name, value)（OkHttp3 覆盖语义）
                header = reqMethod(builder, "header", String.class, String.class);
            }
            if (header == null) {
                return request;
            }
            Method set = header;
            set.invoke(builder, "traceparent", TraceContext.currentTraceparent());
            injectStressHeader(v -> {
                try {
                    set.invoke(builder, "X-Perf-Stress-Task-Id", v);
                } catch (Throwable ignored) {
                    // 静默
                }
            });
            Method build = reqMethod(builder, "build");
            return build != null ? build.invoke(builder) : request;
        } catch (Throwable ignored) {
            return request;
        }
    }

    /**
     * Spring {@code ClientHttpRequest} 注入：{@code getHeaders().set(...)}。
     * （HttpHeaders.set 为覆盖语义，防重复头。）
     */
    public static void injectSpring(Object request) {
        try {
            if (request == null || !hasPerfContext()) {
                return;
            }
            Method headers = reqMethod(request, "getHeaders");
            if (headers == null) {
                return;
            }
            Object h = headers.invoke(request);
            if (h == null) {
                return;
            }
            Method set = reqMethod(h, "set", String.class, String.class);
            if (set == null) {
                return;
            }
            set.invoke(h, "traceparent", TraceContext.currentTraceparent());
            injectStressHeader(v -> {
                try {
                    set.invoke(h, "X-Perf-Stress-Task-Id", v);
                } catch (Throwable ignored) {
                    // 静默
                }
            });
        } catch (Throwable ignored) {
            // 注入失败静默：不影响业务请求
        }
    }
}
