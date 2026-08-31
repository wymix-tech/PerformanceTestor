package com.perfagent.instrumentation;

import net.bytebuddy.asm.Advice;

import java.lang.reflect.Method;

/**
 * Redis 命令拦截 Advice：Lettuce 同步 API 与 Jedis 命令接口。
 * 仅记录耗时与命令文本，超阈值（{@code slowCommandThresholdMs}）才上报。
 */
public class RedisInterceptor {

    @Advice.OnMethodEnter
    public static Object enter(@Advice.AllArguments Object[] args) {
        return MiddlewareSupport.enter(System.nanoTime(), args);
    }

    @Advice.OnMethodExit(onThrowable = Throwable.class)
    public static void exit(@Advice.Enter Object enter, @Advice.Origin Method method) {
        MiddlewareSupport.report(MiddlewareSupport.TYPE_REDIS, enter, method);
    }
}
