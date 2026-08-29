package com.perfagent.trace;

import com.perfagent.reporter.MetricsReporter;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 调用链上下文：通过单个 ThreadLocal 上下文维护每个线程的调用栈，构建 TraceNode 树。
 *
 * <p>工作方式：方法进入时 push 节点并记录开始时间；方法退出时 pop 并计算耗时，
 * 将节点挂到父节点下；栈空（根方法退出）时整棵树提交给 {@link MetricsReporter} 上报。
 *
 * <p>低开销设计：
 * <ul>
 *   <li>全部状态收敛在一个 ThreadLocal 的 {@link Ctx} 对象内——方法热路径仅 1 次
 *       ThreadLocal.get（此前为 4 个独立 ThreadLocal 的 3~4 次 get）；</li>
 *   <li>未采样链路不创建任何上下文对象，中间方法一次判空即短路；</li>
 *   <li>树对象（TraceNode）在业务线程仅做 O(1) 栈操作；转 Map/序列化全部在
 *       reporter 线程完成（见 {@link MetricsReporter}），业务线程零转换开销；</li>
 *   <li>顺带维护节点计数（nodeCount），供 reporter 做字节预算估算，避免提交时再遍历树。</li>
 * </ul>
 */
public final class TraceContext {

    /** 每线程链路上下文（单 ThreadLocal，热路径仅 1 次 get） */
    private static final ThreadLocal<Ctx> CTX = new ThreadLocal<>();

    /** 链路 ID 序号生成器 */
    private static final AtomicLong TRACE_SEQ = new AtomicLong(0);

    /** 采样器（由 AgentPremain 初始化） */
    private static volatile TraceSampler sampler = new TraceSampler(0.1);
    /** 应用名称（由 AgentPremain 初始化） */
    private static volatile String appName = "unknown-app";
    /** 上报器（由 AgentPremain 初始化，用于提交完成的调用链） */
    private static volatile MetricsReporter reporter;

    /** 每线程链路状态：栈、采样标志、链路 ID、根开始时间与节点计数 */
    private static final class Ctx {
        final Deque<TraceNode> stack = new ArrayDeque<>();
        boolean sampled;
        String traceId;
        long rootStartNanos;
        int nodeCount;
    }

    private TraceContext() {
    }

    public static void init(String appName, TraceSampler traceSampler, MetricsReporter metricsReporter) {
        TraceContext.appName = appName;
        sampler = traceSampler;
        reporter = metricsReporter;
    }

    /** 是否为根方法（当前线程无调用上下文） */
    public static boolean isRoot() {
        return CTX.get() == null;
    }

    /** 根方法进入：决定是否采样并初始化链路，返回链路 ID（未采样返回 null，不创建上下文） */
    public static String beginRoot(String method) {
        Ctx existing = CTX.get();
        if (existing != null) {
            // 异常嵌套场景：复用已有上下文与采样标志
            return existing.traceId;
        }
        boolean sampled = sampler.shouldSample();
        if (!sampled) {
            // 未采样：不创建 Ctx（零分配），中间方法经 isRoot/inSampledContext 判空短路
            return null;
        }
        Ctx ctx = new Ctx();
        ctx.sampled = true;
        ctx.traceId = nextTraceId();
        ctx.rootStartNanos = System.nanoTime();
        ctx.nodeCount = 1;
        ctx.stack.push(new TraceNode(method));
        CTX.set(ctx);
        return ctx.traceId;
    }

    /** 方法进入（非根）：已采样才压栈，并记录相对根节点的开始偏移 */
    public static void begin(String method) {
        Ctx ctx = CTX.get();
        if (ctx == null || !ctx.sampled) {
            return;
        }
        TraceNode node = new TraceNode(method);
        node.offsetMs = Math.max(0.0, (System.nanoTime() - ctx.rootStartNanos) / 1_000_000.0);
        ctx.stack.push(node);
        ctx.nodeCount++;
    }

    /** 方法退出：计算耗时并挂接父节点；根方法退出时提交整条链路 */
    public static void end(long startNanos) {
        Ctx ctx = CTX.get();
        if (ctx == null || !ctx.sampled) {
            return;
        }
        Deque<TraceNode> stack = ctx.stack;
        if (stack.isEmpty()) {
            cleanup();
            return;
        }
        TraceNode node = stack.pop();
        node.costMs = Math.max(0.0, (System.nanoTime() - startNanos) / 1_000_000.0);

        if (stack.isEmpty()) {
            // 根节点完成：提交整条链路（树对象直接入队，转 Map 在 reporter 线程做）
            if (reporter != null) {
                reporter.reportTrace(ctx.traceId, appName, node, ctx.nodeCount, System.currentTimeMillis());
            }
            cleanup();
        } else {
            stack.peek().children.add(node);
        }
    }

    /** 清理线程上下文（每次链路结束或异常兜底） */
    public static void cleanup() {
        CTX.remove();
    }

    /** 生成链路 ID */
    public static String nextTraceId() {
        return "trace-" + System.currentTimeMillis() + "-" + TRACE_SEQ.incrementAndGet();
    }

    /** 判断当前线程是否处于采样链路中（供 JDBC 拦截等复用采样标志） */
    public static boolean inSampledContext() {
        Ctx ctx = CTX.get();
        return ctx != null && ctx.sampled && !ctx.stack.isEmpty();
    }

    /**
     * 当前调用方法链快照（根 → 叶方法名列表），供慢 SQL 等事件记录触发链路。
     * 仅在慢 SQL 阈值命中后调用（JDBC 拦截处），正常路径零开销。
     */
    public static List<String> currentMethodChain() {
        Deque<TraceNode> stack = CTX.get() == null ? null : CTX.get().stack;
        if (stack == null || stack.isEmpty()) {
            return List.of();
        }
        List<String> chain = new ArrayList<>(stack.size());
        for (java.util.Iterator<TraceNode> it = stack.descendingIterator(); it.hasNext(); ) {
            chain.add(it.next().method);
        }
        return chain;
    }
}
