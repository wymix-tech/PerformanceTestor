package com.perfagent.instrumentation;

import net.bytebuddy.asm.Advice;

import java.lang.reflect.Method;

/**
 * RabbitMQ 拦截 Advice：拦截 {@code com.rabbitmq.client.Channel} 的发布/消费/声明等命令。
 * 消息体（byte[]）仅解码前 256 字节，避免大消息拖慢上报路径。
 */
public class MqInterceptor {

    @Advice.OnMethodEnter
    public static Object enter(@Advice.AllArguments Object[] args) {
        return MiddlewareSupport.enter(System.nanoTime(), args);
    }

    @Advice.OnMethodExit(onThrowable = Throwable.class)
    public static void exit(@Advice.Enter Object enter, @Advice.Origin Method method) {
        MiddlewareSupport.report(MiddlewareSupport.TYPE_MQ, enter, method);
    }
}
