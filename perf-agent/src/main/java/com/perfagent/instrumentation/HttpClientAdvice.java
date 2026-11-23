package com.perfagent.instrumentation;

import net.bytebuddy.asm.Advice;

/**
 * HTTP 客户端出口拦截 Advice（P2-2，httpClientEnabled=true 时启用）：
 * 在请求发出前注入 {@code traceparent} 与 {@code X-Perf-Stress-Task-Id}。
 *
 * <p>Advice 内联约束：仅可调用 {@link HttpClientSupport} 的 public static 方法，
 * 全部反射实现（agent 零编译期依赖）。四类常见同步客户端：
 * <ul>
 *   <li>Apache：{@code HttpClient#execute} 首参 {@code HttpUriRequest} 可变请求对象直接注入；</li>
 *   <li>OkHttp：{@code OkHttpClient#newCall(Request)} 经 Builder 重建替换入参；</li>
 *   <li>JDK：{@code HttpClient#send/sendAsync(HttpRequest, ...)} 经 Builder 重建替换入参；</li>
 *   <li>Spring：{@code ClientHttpRequest#execute()} 对 receiver 的 headers 注入。</li>
 * </ul>
 */
public final class HttpClientAdvice {

    private HttpClientAdvice() {
    }

    /** Apache HttpClient：execute(...) 首参为请求对象，直接 addHeader */
    public static class Apache {
        @Advice.OnMethodEnter
        public static void enter(@Advice.Argument(0) Object request) {
            HttpClientSupport.injectApache(request);
        }
    }

    /**
     * OkHttp / JDK HttpClient：请求不可变，重建后写回参数槽
     * （{@code readOnly = false} 允许 Advice 写回入参）。
     */
    public static class Rebuilding {
        @Advice.OnMethodEnter
        public static void enter(@Advice.Argument(value = 0, readOnly = false) Object request) {
            Object replaced = HttpClientSupport.injectRequest(request);
            if (replaced != null && replaced != request) {
                request = replaced; // 写回参数槽：后续真实调用使用携带链路头的请求
            }
        }
    }

    /** Spring ClientHttpRequest：对 receiver 的 headers 注入 */
    public static class Spring {
        @Advice.OnMethodEnter
        public static void enter(@Advice.This Object receiver) {
            HttpClientSupport.injectSpring(receiver);
        }
    }
}
