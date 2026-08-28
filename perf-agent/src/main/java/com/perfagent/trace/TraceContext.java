package com.perfagent.trace;

import com.perfagent.reporter.MetricsReporter;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 调用链上下文：通过 ThreadLocal 维护每个线程的调用栈，构建 TraceNode 树。
 *
 * <p>工作方式：方法进入时 push 节点并记录开始时间；方法退出时 pop 并计算耗时，
 * 将节点挂到父节点下；栈空（根方法退出）时整棵树提交给 {@link MetricsReporter} 上报。
 *
 * <p>埋点统计与上报完全异步，业务线程除 ThreadLocal 操作外零额外开销。
 */
public final class TraceContext {

    /** 每个线程的调用栈 */
    private static final ThreadLocal<Deque<TraceNode>> STACK = new ThreadLocal<>();
    /** 本次链路是否采样 */
    private static final ThreadLocal<Boolean> SAMPLED = new ThreadLocal<>();
    /** 本次链路 ID（根方法生成，全链路共享） */
    private static final ThreadLocal<String> TRACE_ID = new ThreadLocal<>();
    /** 根方法进入时间（nanoTime，用于计算各节点相对根节点的开始偏移） */
    private static final ThreadLocal<Long> ROOT_START = new ThreadLocal<>();

    /** 链路 ID 序号生成器 */
    private static final AtomicLong TRACE_SEQ = new AtomicLong(0);

    /** 采样器（由 AgentPremain 初始化） */
    private static volatile TraceSampler sampler = new TraceSampler(0.1);
    /** 应用名称（由 AgentPremain 初始化） */
    private static volatile String appName = "unknown-app";
    /** 上报器（由 AgentPremain 初始化，用于提交完成的调用链） */
    private static volatile MetricsReporter reporter;

    private TraceContext() {
    }

    public static void init(String appName, TraceSampler traceSampler, MetricsReporter metricsReporter) {
        TraceContext.appName = appName;
        sampler = traceSampler;
        reporter = metricsReporter;
    }

    /** 是否为根方法（当前线程无调用上下文） */
    public static boolean isRoot() {
        return STACK.get() == null;
    }

    /** 根方法进入：决定是否采样并初始化链路，返回链路 ID（未采样返回 null） */
    public static String beginRoot(String method) {
        if (STACK.get() != null) {
            // 异常嵌套场景：复用已有上下文与采样标志
            return TRACE_ID.get();
        }
        boolean sampled = sampler.shouldSample();
        SAMPLED.set(sampled);
        if (!sampled) {
            return null;
        }
        String traceId = nextTraceId();
        TRACE_ID.set(traceId);
        ROOT_START.set(System.nanoTime());
        Deque<TraceNode> stack = new ArrayDeque<>();
        stack.push(new TraceNode(method));
        STACK.set(stack);
        return traceId;
    }

    /** 方法进入（非根）：若已采样则压栈，并记录相对根节点的开始偏移 */
    public static void begin(String method) {
        if (SAMPLED.get() == Boolean.TRUE) {
            TraceNode node = new TraceNode(method);
            Long rootStart = ROOT_START.get();
            node.offsetMs = rootStart == null ? 0.0 : Math.max(0.0, (System.nanoTime() - rootStart) / 1_000_000.0);
            STACK.get().push(node);
        }
    }

    /** 方法退出：计算耗时并挂接父节点；根方法退出时提交整条链路 */
    public static void end(long startNanos) {
        if (SAMPLED.get() != Boolean.TRUE) {
            return;
        }
        Deque<TraceNode> stack = STACK.get();
        if (stack == null || stack.isEmpty()) {
            cleanup();
            return;
        }
        TraceNode node = stack.pop();
        node.costMs = Math.max(0.0, (System.nanoTime() - startNanos) / 1_000_000.0);

        if (stack.isEmpty()) {
            // 根节点完成：提交整条链路
            if (reporter != null) {
                reporter.reportTrace(TRACE_ID.get(), appName, node, System.currentTimeMillis());
            }
            cleanup();
        } else {
            stack.peek().children.add(node);
        }
    }

    /** 清理线程上下文（每次链路结束或异常兜底） */
    public static void cleanup() {
        STACK.remove();
        SAMPLED.remove();
        TRACE_ID.remove();
        ROOT_START.remove();
    }

    /** 生成链路 ID */
    public static String nextTraceId() {
        return "trace-" + System.currentTimeMillis() + "-" + TRACE_SEQ.incrementAndGet();
    }

    /** 判断当前线程是否处于采样链路中（供 JDBC 拦截等复用采样标志） */
    public static boolean inSampledContext() {
        return SAMPLED.get() == Boolean.TRUE && STACK.get() != null && !STACK.get().isEmpty();
    }

    /**
     * 当前调用方法链快照（根 → 叶方法名列表），供慢 SQL 等事件记录触发链路。
     * 在业务线程内调用（JDBC 拦截处），直接读取 ThreadLocal 栈，无锁无复制开销之外仅一次遍历。
     */
    public static java.util.List<String> currentMethodChain() {
        Deque<TraceNode> stack = STACK.get();
        if (stack == null || stack.isEmpty()) {
            return java.util.List.of();
        }
        java.util.List<String> chain = new java.util.ArrayList<>(stack.size());
        for (java.util.Iterator<TraceNode> it = stack.descendingIterator(); it.hasNext(); ) {
            chain.add(it.next().method);
        }
        return chain;
    }
}
