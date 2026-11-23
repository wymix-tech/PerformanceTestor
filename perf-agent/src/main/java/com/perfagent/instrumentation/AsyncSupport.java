package com.perfagent.instrumentation;

import com.perfagent.trace.TraceContext;

/**
 * 异步上下文透传支持（P1-5）：跨线程池任务提交时快照/恢复调用链与压测染色上下文。
 *
 * <p>设计约束：
 * <ul>
 *   <li>零业务依赖：Runnable 替换基于 Object + 接口实现，无任何第三方类型；</li>
 *   <li>开关默认关闭（{@code asyncTraceEnabled=false}），由 AgentPremain 按配置打开；</li>
 *   <li>所有入口全量 {@code try/catch (Throwable)} 兜底，绝不影响被测应用业务。</li>
 * </ul>
 */
public final class AsyncSupport {

    /** 是否启用异步透传（AgentPremain 按 asyncTraceEnabled 配置写入；volatile 保证可见性） */
    public static volatile boolean enabled = false;

    private AsyncSupport() {
    }

    /**
     * 包装待提交任务：当前线程存在可透传上下文（采样链路或压测染色）时，
     * 用携带快照的 Runnable 替换原任务；否则原样返回（零包装零开销）。
     */
    public static Runnable wrap(Runnable task) {
        if (!enabled || task == null) {
            return task;
        }
        try {
            if (task instanceof AsyncRunnable) {
                return task; // 二次提交防重复包装
            }
            TraceContext.Snapshot snapshot = TraceContext.capture();
            if (snapshot == null) {
                return task; // 无上下文或无需透传
            }
            return new AsyncRunnable(task, snapshot);
        } catch (Throwable ignored) {
            return task; // 兜底：包装失败不影响任务提交
        }
    }

    /** 携带快照的任务包装：子线程执行前 attach、finally 中 detach（防池复用串用） */
    static final class AsyncRunnable implements Runnable {
        private final Runnable delegate;
        private final TraceContext.Snapshot snapshot;

        AsyncRunnable(Runnable delegate, TraceContext.Snapshot snapshot) {
            this.delegate = delegate;
            this.snapshot = snapshot;
        }

        @Override
        public void run() {
            TraceContext.Snapshot prev = TraceContext.attach(snapshot);
            try {
                delegate.run(); // 业务异常原样上抛（保持 Executor 语义），不吞不裹
            } finally {
                TraceContext.detach(prev); // 严禁遗留 CTX：池复用线程防上下文污染
            }
        }
    }
}
