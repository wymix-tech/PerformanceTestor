package com.perfagent.instrumentation;

import net.bytebuddy.asm.Advice;

import java.lang.reflect.Method;

/**
 * Elasticsearch 客户端拦截 Advice：拦截同步 {@code RestClient#performRequest}（HTTP 出口）。
 * RestHighLevelClient 的同步 search/index/bulk 最终都汇聚到 RestClient，故只拦此处可避免
 * 高层 API 与底层调用重复计数。
 *
 * <p><b>已知限制</b>：不覆盖 {@code performRequestAsync}——其返回 Future，
 * {@code @Advice.OnMethodExit} 触发时请求尚未真正执行，只能测到"提交异步请求"的耗时，
 * 慢查询会漏报。覆盖异步路径需在 Future 完成回调处计时，要求 agent 侧引入 ES 客户端类型，
 * 故本期只覆盖同步路径（埋点选择见 {@code TraceEnhancer#builder} ES 段注释）。
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
