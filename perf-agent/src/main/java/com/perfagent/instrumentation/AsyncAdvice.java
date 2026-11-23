package com.perfagent.instrumentation;

import net.bytebuddy.asm.Advice;

/**
 * {@code Executor#execute(Runnable)} 入参替换 Advice（P1-5 异步透传，默认关闭）。
 *
 * <p>内联到 Executor 实现类的 execute 方法：把入参 Runnable 替换为携带
 * {@link TraceContext.Snapshot} 快照的包装任务（见 {@link AsyncSupport#wrap}），
 * 子线程执行前恢复上下文、finally 中清理。
 *
 * <p>注意：
 * <ul>
 *   <li>参数写入依赖 {@code @Advice.Argument(readOnly = false)}（ByteBuddy 支持的
 *       内联参数替换语义），替换仅发生在方法体执行前的局部变量槽；</li>
 *   <li>Advice 类与辅助方法均为 public static + 全量兜底，不影响业务；</li>
 *   <li>类型侧匹配排除 agent 自身类与 JDK ForkJoinPool（防类加载死锁与递归增强），
 *       见 {@code TraceEnhancer#builder}。</li>
 * </ul>
 */
public class AsyncAdvice {

    private AsyncAdvice() {
    }

    @Advice.OnMethodEnter
    public static void enter(@Advice.Argument(value = 0, readOnly = false) Runnable task) {
        task = AsyncSupport.wrap(task);
    }
}
