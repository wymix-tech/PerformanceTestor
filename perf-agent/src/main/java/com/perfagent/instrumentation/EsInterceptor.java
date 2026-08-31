package com.perfagent.instrumentation;

import net.bytebuddy.asm.Advice;

import java.lang.reflect.Method;

/**
 * Elasticsearch 客户端拦截 Advice：拦截 {@code RestClient#performRequest*}（HTTP 出口）。
 * RestHighLevelClient 的 search/index/bulk 最终都汇聚到 RestClient，故只拦此处可避免
 * 高层 API 与底层调用重复计数。
 */
public class EsInterceptor {

    @Advice.OnMethodEnter
    public static Object enter(@Advice.AllArguments Object[] args) {
        return MiddlewareSupport.enter(System.nanoTime(), args);
    }

    @Advice.OnMethodExit(onThrowable = Throwable.class)
    public static void exit(@Advice.Enter Object enter, @Advice.Origin Method method) {
        MiddlewareSupport.report(MiddlewareSupport.TYPE_ES, enter, method);
    }
}
