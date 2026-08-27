package com.perfagent.instrumentation;

import com.perfagent.trace.TraceContext;
import net.bytebuddy.asm.Advice;

/**
 * 业务方法耗时埋点 Advice（ByteBuddy 内联到目标方法）。
 *
 * <p>方法进入时入栈、退出时出栈并计算耗时，构建调用链树（由 {@link TraceContext} 维护）。
 * 根方法（Controller 入口）进入时决定是否采样并生成链路 ID。
 *
 * <p>Advice 内联要求：本类方法体引用的静态方法必须是可内联的普通调用，
 * {@link TraceContext} 的静态方法均满足此要求。
 */
public class MethodTimerAdvice {

    /** OnMethodEnter 返回值：携带链路 ID 与开始时间（null 表示本次调用不采样） */
    public static class EnterResult {
        public final String traceId;
        public final long startNanos;

        public EnterResult(String traceId, long startNanos) {
            this.traceId = traceId;
            this.startNanos = startNanos;
        }
    }

    /**
     * 方法进入：入栈并记录开始时间。
     *
     * @param origin 方法签名（类名#方法名）
     * @return EnterResult 或 null（未采样）
     */
    @Advice.OnMethodEnter
    public static Object enter(@Advice.Origin("#t.#m") String origin) {
        if (TraceContext.isRoot()) {
            // 根方法：决定采样并初始化链路
            String traceId = TraceContext.beginRoot(origin);
            return traceId == null ? null : new EnterResult(traceId, System.nanoTime());
        }
        if (TraceContext.inSampledContext()) {
            TraceContext.begin(origin);
            return new EnterResult(null, System.nanoTime());
        }
        return null;
    }

    /**
     * 方法退出：出栈、计算耗时并挂接父节点；根方法退出时提交整条链路。
     * onThrowable 保证异常路径也会执行，避免调用栈泄漏。
     */
    @Advice.OnMethodExit(onThrowable = Throwable.class)
    public static void exit(@Advice.Enter Object enter) {
        if (enter == null) {
            return;
        }
        EnterResult er = (EnterResult) enter;
        TraceContext.end(er.startNanos);
    }
}
