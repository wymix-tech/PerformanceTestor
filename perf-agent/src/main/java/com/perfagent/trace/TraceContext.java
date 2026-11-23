package com.perfagent.trace;

import com.perfagent.reporter.MetricsReporter;

import java.lang.ClassValue;
import java.lang.reflect.Method;
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

    /** 链路 ID 序号生成器（P2-2 前旧格式遗留；现 traceId 为随机 hex，保留防外部引用） */
    private static final AtomicLong TRACE_SEQ = new AtomicLong(0);

    /**
     * 压测流量染色请求头（P1-1）：压测引擎统一注入 {@code X-Perf-Stress-Task-Id}，
     * 探针在 Filter 根入口零依赖捕获（兼容 javax/jakarta Servlet）。
     */
    static final String STRESS_TASK_ID_HEADER = "X-Perf-Stress-Task-Id";

    /**
     * W3C 跨服务链路头（P2-2）：上游服务出口拦截器注入的标准 {@code traceparent}，
     * 格式 {@code 00-<32hex traceId>-<16hex parentSpanId>-<2hex flags>}。
     */
    static final String TRACEPARENT_HEADER = "traceparent";

    /**
     * getHeader(String) 反射方法缓存：按 request 实际类缓存（ClassValue，热路径 O(1)）。
     * 零依赖兼容 javax.servlet / jakarta.servlet 及任意带 getHeader 的请求包装类；
     * 类上无 getHeader（非 Servlet 容器 / 单测 MockHttpServletRequest）时缓存 null 直接短路。
     */
    private static final ClassValue<Method> GET_HEADER_CACHE = new ClassValue<>() {
        @Override
        protected Method computeValue(Class<?> type) {
            // P0 修复（Fix-5）：非 public ServletRequest 包装类上，getMethod 可查得
            // 方法但 invoke 抛 IllegalAccessException 被上层静默吞掉 → 染色头变 null。
            // 优先取类方法并 setAccessible(true)；受模块限制或查找失败时回退遍历
            // public 接口上的 getHeader(String)（接口反射调用不受实现类可见性限制）。
            Method declared = null;
            try {
                declared = type.getMethod("getHeader", String.class);
            } catch (Throwable ignored) {
                // 类上不可查得：走接口回退
            }
            if (declared != null) {
                try {
                    declared.setAccessible(true);
                    return declared;
                } catch (Throwable ignored) {
                    // setAccessible 受模块限制：回退接口方法
                }
            }
            for (Class<?> iface : type.getInterfaces()) {
                try {
                    return iface.getMethod("getHeader", String.class);
                } catch (Throwable ignored) {
                    // 继续尝试下一个接口
                }
            }
            return null;
        }
    };

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
        /** Filter 根模式：上下文生命周期由最外层 Filter 控制（endFilterRoot 清理） */
        final boolean filterOwned;
        /** 本请求主链路已提交（Filter 模式：一请求至多一条链路，抑制后置噪音） */
        boolean chainSubmitted;
        String traceId;
        long rootStartNanos;
        int nodeCount;
        /** 压测染色任务 ID（P1-1：与采样决策无关，未采样请求同样携带，供慢 SQL/慢命令关联） */
        String stressTaskId;
        /** 本服务 span ID（P2-2，16 位 hex；非 Filter 根入口为 null → 服务端缺省 "root"） */
        String spanId;
        /** 上游服务 span ID（P2-2，继承 traceparent；无上游为 null） */
        String parentSpanId;

        Ctx(boolean filterOwned) {
            this.filterOwned = filterOwned;
        }
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
        Ctx ctx = new Ctx(false);
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
        if (ctx.stack.isEmpty()) {
            if (ctx.chainSubmitted) {
                // 本请求主链路已提交：后置处理（afterCompletion 等）不再开启噪音链路
                return;
            }
            // Filter 根模式：首个业务方法即调用链根节点（Controller 等），
            // 重置根基准时间保证根节点 offset=0
            ctx.rootStartNanos = System.nanoTime();
            ctx.nodeCount = 0;
        }
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
            // 防御路径：无配对 begin（增强异常等罕见场景）；Filter 模式由 Filter 退出兜底清理
            if (!ctx.filterOwned) {
                cleanup();
            }
            return;
        }
        TraceNode node = stack.pop();
        node.costMs = Math.max(0.0, (System.nanoTime() - startNanos) / 1_000_000.0);

        if (stack.isEmpty()) {
            // 根节点完成：提交整条链路（树对象直接入队，转 Map 在 reporter 线程做）
            if (reporter != null) {
                reporter.reportTrace(ctx.traceId, appName, node, ctx.nodeCount,
                        System.currentTimeMillis(), ctx.stressTaskId,
                        ctx.spanId, ctx.parentSpanId);
            }
            ctx.chainSubmitted = true;
            // Filter 根模式：上下文保留至 Filter 退出（sampled 标志仍有效，
            // 后续方法不再重复成为根）；旧模式（无 Filter）：根方法退出即清理
            if (!ctx.filterOwned) {
                cleanup();
            }
        } else {
            stack.peek().children.add(node);
        }
    }

    /**
     * Filter 根进入（HTTP 请求入口，由 {@code FilterRootAdvice} 调用）：
     * 当前线程无上下文时做<b>一次性采样决策</b>并占位（栈暂空，等首个业务方法成为根节点）。
     *
     * <p>修复根语义缺陷：此前采样决策发生在"首个业务方法"，Controller 未命中采样时
     * 下游每个方法都会重新尝试成为根（采样稀释 + 半截链路）。Filter 根模式下：
     * <ul>
     *   <li>命中采样：创建上下文（sampled=true，栈空），首个业务方法成为完整链路根；</li>
     *   <li>未命中：创建 NOT_SAMPLED 占位上下文（sampled=false），下游业务方法
     *       经 {@link #inSampledContext()} 一次判断即整链短路，不再重复采样。</li>
     * </ul>
     *
     * @return true = 调用方为本链路 owner（最外层 Filter），退出时须调 {@link #endFilterRoot()}；
     *         false = 嵌套 Filter 或上下文已存在，无需处理
     */
    public static boolean beginFilterRoot() {
        return beginFilterRoot(null);
    }

    /**
     * Filter 根进入（P1-1 增强重载）：由 {@code FilterRootAdvice} 传入请求对象，
     * 在采样决策<b>之前</b>捕获压测染色头 {@code X-Perf-Stress-Task-Id}。
     *
     * <p>关键语义：无论 {@code sampler.shouldSample()} 命中与否，{@code stressTaskId}
     * 都必须存入上下文——慢 SQL（JdbcInterceptor）与慢命令（MiddlewareSupport）独立于
     * 调用链采样率触发，未采样请求产生的慢 SQL 同样必须携带染色标识。
     *
     * @param request Servlet 请求对象（{@code javax/jakarta.servlet.ServletRequest}），
     *                为 null 或非 Servlet 请求时染色标识为 null（可选字段，向后兼容）
     */
    public static boolean beginFilterRoot(Object request) {
        Ctx existing = CTX.get();
        if (existing != null) {
            return false;
        }
        Ctx ctx = new Ctx(true);
        ctx.stressTaskId = extractStressTaskId(request);
        // P2-2 W3C 跨服务串联：合法 traceparent → 继承上游 traceId 并记录 parentSpanId；
        // 无/非法 → 生成本服务新 32 位 hex traceId。spanId 无论采样与否都生成
        // （跨服务串联需要每个服务独立 span，采样率只影响调用链明细是否上报）。
        String[] tp = parseTraceparent(extractHeader(request, TRACEPARENT_HEADER));
        if (tp != null) {
            ctx.traceId = tp[0];
            ctx.parentSpanId = tp[1];
        } else {
            ctx.traceId = nextTraceId();
        }
        ctx.spanId = nextSpanId();
        if (sampler.shouldSample()) {
            ctx.sampled = true;
            ctx.rootStartNanos = System.nanoTime();
            ctx.nodeCount = 0;
        } else {
            ctx.sampled = false; // NOT_SAMPLED 占位：整链短路标记
        }
        CTX.set(ctx);
        return true;
    }

    /**
     * 通用请求头提取（P2-2）：复用 {@link #GET_HEADER_CACHE} 反射缓存，
     * 与 {@link #extractStressTaskId(Object)} 同一条零依赖路径。
     */
    private static String extractHeader(Object request, String name) {
        if (request == null) {
            return null;
        }
        try {
            Method m = GET_HEADER_CACHE.get(request.getClass());
            if (m == null) {
                return null;
            }
            Object v = m.invoke(request, name);
            return v == null ? null : v.toString();
        } catch (Throwable ignored) {
            return null;
        }
    }

    /**
     * W3C traceparent 解析（P2-2）：{@code 00-<32hex>-<16hex>-<2hex>}。
     * 合法返回 {traceId, parentSpanId}；版本 ff / ID 全零 / 格式非法返回 null（当作无上游）。
     */
    static String[] parseTraceparent(String header) {
        if (header == null) {
            return null;
        }
        String s = header.trim();
        if (s.length() != 55 || s.charAt(2) != '-' || s.charAt(35) != '-' || s.charAt(52) != '-') {
            return null;
        }
        String version = s.substring(0, 2);
        String traceId = s.substring(3, 35);
        String spanId = s.substring(36, 52);
        String flags = s.substring(53);
        if (!isLowerHex(version) || !isLowerHex(traceId) || !isLowerHex(spanId) || !isLowerHex(flags)) {
            return null;
        }
        if ("ff".equals(version) || isAllZero(traceId) || isAllZero(spanId)) {
            return null;
        }
        return new String[]{traceId, spanId};
    }

    private static boolean isLowerHex(String s) {
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if ((c < '0' || c > '9') && (c < 'a' || c > 'f')) {
                return false;
            }
        }
        return true;
    }

    private static boolean isAllZero(String s) {
        for (int i = 0; i < s.length(); i++) {
            if (s.charAt(i) != '0') {
                return false;
            }
        }
        return true;
    }

    /**
     * 零依赖提取压测染色头：反射调用 {@code request.getHeader(String)}，
     * 方法引用按 request 实际类经 {@link #GET_HEADER_CACHE} 缓存（热路径 O(1)）。
     * 必须为 public static（被 Advice 内联代码引用），内部全量兜底，绝不影响业务。
     */
    public static String extractStressTaskId(Object request) {
        if (request == null) {
            return null;
        }
        try {
            Method m = GET_HEADER_CACHE.get(request.getClass());
            if (m == null) {
                return null;
            }
            Object v = m.invoke(request, STRESS_TASK_ID_HEADER);
            return v == null ? null : v.toString();
        } catch (Throwable ignored) {
            // 非 Servlet 容器、方法不可达、反射受限等场景：染色标识缺省为空
            return null;
        }
    }

    /**
     * Filter 根退出（仅 owner 调用）：清理本链路上下文。
     *
     * <p>栈空（未采样 / 请求未触达业务方法，如静态资源）时无 trace 提交；
     * 栈非空说明存在未配对退出的业务方法（异常增强失败等罕见场景），直接丢弃防泄漏。
     */
    public static void endFilterRoot() {
        if (CTX.get() != null) {
            cleanup();
        }
    }

    /** 清理线程上下文（每次链路结束或异常兜底） */
    public static void cleanup() {
        CTX.remove();
    }

    /** 生成链路 ID（P2-2：W3C 规范 32 位小写 hex；旧 trace-ts-seq 格式存量数据无缝兼容） */
    public static String nextTraceId() {
        return randomHex(16);
    }

    /** 生成 span ID（W3C 规范 16 位小写 hex） */
    public static String nextSpanId() {
        return randomHex(8);
    }

    /** ThreadLocalRandom → n 字节小写 hex（无锁、无网络 IO，热路径安全） */
    private static String randomHex(int bytes) {
        java.util.concurrent.ThreadLocalRandom rnd = java.util.concurrent.ThreadLocalRandom.current();
        char[] chars = new char[bytes * 2];
        for (int i = 0; i < chars.length; i += 2) {
            int b = rnd.nextInt(256);
            chars[i] = HEX[b >>> 4];
            chars[i + 1] = HEX[b & 0xF];
        }
        return new String(chars);
    }

    private static final char[] HEX = "0123456789abcdef".toCharArray();

    /** 判断当前线程是否处于采样链路中（供 JDBC 拦截等复用采样标志） */
    public static boolean inSampledContext() {
        Ctx ctx = CTX.get();
        // Filter 根模式下栈可能暂空（业务方法尚未进入），仅按 sampled 标志判断
        return ctx != null && ctx.sampled;
    }

    /**
     * 当前链路 ID（未采样/无上下文时返回 null），供慢 SQL 与慢命令事件关联调用链。
     * 仅在阈值命中后调用（慢路径），正常路径零开销。
     */
    public static String currentTraceId() {
        Ctx ctx = CTX.get();
        return ctx == null ? null : ctx.traceId;
    }

    /**
     * 当前压测染色任务 ID（P1-1）：供慢 SQL / 慢命令上报关联压测任务。
     * 与采样决策无关（Filter 根入口已无条件捕获）；无上下文/非压测流量返回 null。
     * 仅在阈值命中后调用（慢路径），正常路径零开销。
     */
    public static String currentStressTaskId() {
        Ctx ctx = CTX.get();
        return ctx == null ? null : ctx.stressTaskId;
    }

    /**
     * 当前 W3C traceparent（P2-2）：供出口拦截器注入下游请求。
     * 格式 {@code 00-<traceId>-<spanId>-01}；无上下文 / 无 traceId 或 spanId 时返回 null。
     * 仅在 HTTP 客户端请求发出前调用（慢路径），正常路径零开销。
     */
    public static String currentTraceparent() {
        Ctx ctx = CTX.get();
        if (ctx == null || ctx.traceId == null || ctx.spanId == null) {
            return null;
        }
        return "00-" + ctx.traceId + "-" + ctx.spanId + "-01";
    }

    /** 当前 span ID（P2-2，上报 trace_chain.span_id；无上下文返回 null → 服务端缺省 "root"） */
    public static String currentSpanId() {
        Ctx ctx = CTX.get();
        return ctx == null ? null : ctx.spanId;
    }

    /** 当前上游 span ID（P2-2，上报 trace_chain.parent_span_id；无上游返回 null） */
    public static String currentParentSpanId() {
        Ctx ctx = CTX.get();
        return ctx == null ? null : ctx.parentSpanId;
    }

    /**
     * 跨线程上下文快照（P1-5 异步透传）：{@link #capture()} 在提交任务时拍快照，
     * 子线程 {@link #attach(Snapshot)} 恢复、{@link #detach(Snapshot)} 清理。
     */
    public static final class Snapshot {
        final String traceId;
        final String stressTaskId;
        final boolean sampled;
        /** P2-2：span 透传（子线程共享本服务同一 span，parentSpanId 原样继承） */
        final String spanId;
        final String parentSpanId;
        /** 父线程当前栈顶节点（子任务方法挂为它的子调用；未采样为 null） */
        final TraceNode parentNode;
        /**
         * P0 修复（Fix-3）：attach 之前线程原本的 Ctx 引用（原样保留完整调用栈、
         * filterOwned 与 nodeCount）。非 null 时 detach 原样归还该 Ctx，不再重建
         * 单节点 Ctx 破坏外层调用栈；null 表示常规快照（旧语义兼容）。
         */
        final Ctx previousCtx;

        Snapshot(String traceId, String stressTaskId, boolean sampled, TraceNode parentNode) {
            this(traceId, stressTaskId, sampled, parentNode, null);
        }

        private Snapshot(String traceId, String stressTaskId, boolean sampled,
                         TraceNode parentNode, Ctx previousCtx) {
            this(traceId, stressTaskId, sampled, null, null, parentNode, previousCtx);
        }

        private Snapshot(String traceId, String stressTaskId, boolean sampled,
                         String spanId, String parentSpanId,
                         TraceNode parentNode, Ctx previousCtx) {
            this.traceId = traceId;
            this.stressTaskId = stressTaskId;
            this.sampled = sampled;
            this.spanId = spanId;
            this.parentSpanId = parentSpanId;
            this.parentNode = parentNode;
            this.previousCtx = previousCtx;
        }

        /** 包装线程已有 Ctx（attach 不覆盖场景）：detach 时原样归还，不动原栈 */
        static Snapshot ofExisting(Ctx existing) {
            TraceNode top = existing.sampled && !existing.stack.isEmpty()
                    ? existing.stack.peek() : null;
            return new Snapshot(existing.traceId, existing.stressTaskId,
                    existing.sampled, existing.spanId, existing.parentSpanId,
                    top, existing);
        }
    }

    /**
     * 拍摄当前线程上下文快照（任务提交时刻调用，线程池复用安全）。
     * 无上下文 / 上下文既未采样也无染色标识时返回 null（无需透传）。
     */
    public static Snapshot capture() {
        Ctx ctx = CTX.get();
        if (ctx == null || (!ctx.sampled && ctx.stressTaskId == null)) {
            return null;
        }
        return new Snapshot(ctx.traceId, ctx.stressTaskId, ctx.sampled,
                ctx.spanId, ctx.parentSpanId, ctx.sampled ? ctx.stack.peek() : null, null);
    }

    /**
     * 在子线程恢复快照（必须与 {@link #detach(Snapshot)} 配对，finally 中调用）。
     *
     * <p>语义：线程已存在上下文（池复用残留/嵌套提交）时<b>不覆盖</b>并返回其快照；
     * 正常恢复时把子任务方法挂为 {@code parentNode} 的子调用（采样继承），
     * {@code stressTaskId} 无条件继承（未采样压测流量同样透传染色标识）。
     *
     * @return 供 {@link #detach(Snapshot)} 使用的"提交时刻"前上下文快照（可为 null）
     */
    public static Snapshot attach(Snapshot snapshot) {
        Ctx existing = CTX.get();
        if (snapshot == null || existing != null) {
            // 已有上下文不覆盖（防池复用串用）：把原 Ctx 引用暂存进返回值，
            // 供 detach 原样归还（P0 修复：不动原栈，不丢完整调用栈）
            return existing == null ? null : Snapshot.ofExisting(existing);
        }
        Ctx ctx = new Ctx(false); // 非 Filter 根：由 detach/end 负责清理
        ctx.sampled = snapshot.sampled;
        ctx.traceId = snapshot.traceId;
        ctx.stressTaskId = snapshot.stressTaskId;
        ctx.spanId = snapshot.spanId;
        ctx.parentSpanId = snapshot.parentSpanId;
        ctx.rootStartNanos = System.nanoTime();
        if (snapshot.sampled && snapshot.parentNode != null) {
            ctx.stack.push(snapshot.parentNode); // 子任务方法成为父节点子调用
        }
        CTX.set(ctx);
        return null; // attach 前无上下文，detach(null) 直接 CTX.remove()
    }

    /**
     * 清理/恢复子线程上下文（<b>必须</b>在 finally 中调用，严禁在线程池复用线程上
     * 遗留 CTX——否则后续无关请求会串用 stressTaskId/traceId 造成上下文污染）。
     *
     * @param prev {@link #attach(Snapshot)} 返回的快照：非 null 恢复该上下文，
     *             null 直接移除 ThreadLocal
     */
    public static void detach(Snapshot prev) {
        if (prev == null) {
            CTX.remove();
            return;
        }
        if (prev.previousCtx != null) {
            // P0 修复：attach 前线程已有上下文 → 原样恢复外层 Ctx
            // （完整调用栈 / filterOwned / nodeCount 不丢失，不重建残缺栈）
            CTX.set(prev.previousCtx);
            return;
        }
        // 兼容外部直接传入手动 capture() 快照的旧调用
        Ctx ctx = new Ctx(false);
        ctx.sampled = prev.sampled;
        ctx.traceId = prev.traceId;
        ctx.stressTaskId = prev.stressTaskId;
        ctx.spanId = prev.spanId;
        ctx.parentSpanId = prev.parentSpanId;
        ctx.rootStartNanos = System.nanoTime();
        if (prev.sampled && prev.parentNode != null) {
            ctx.stack.push(prev.parentNode);
        }
        CTX.set(ctx);
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
