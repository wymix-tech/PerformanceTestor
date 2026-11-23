package com.perfagent.instrumentation;

import com.perfagent.trace.TraceContext;
import net.bytebuddy.asm.Advice;

/**
 * Servlet Filter 根 Advice：以最外层 Filter#doFilter 作为调用链唯一采样决策点。
 *
 * <p>配合 {@link TraceContext#beginFilterRoot()} 实现整链采样语义：
 * <ul>
 *   <li>请求入口一次性决策（命中 → 完整链路从首个业务方法开始；未命中 → 整链短路）；</li>
 *   <li>嵌套 Filter（非 owner）enter 返回 null，exit 直接跳过，不产生任何开销；</li>
 *   <li>Filter 自身<b>不入调用树</b>——调用链根保持为首个业务方法（Controller），入口语义清晰。</li>
 * </ul>
 *
 * <p>Advice 内联约束：仅调用 {@link TraceContext} 可内联静态方法，
 * enter 返回 {@link Boolean#TRUE} 单例（== 比较）避免每请求对象分配。
 */
public class FilterRootAdvice {

    @Advice.OnMethodEnter
    public static Object enter(@Advice.Argument(0) Object request) {
        // true = 本 Filter 是最外层且创建了链路上下文（owner）；
        // null = 嵌套 Filter 或上下文已存在（非 owner，零处理）。
        // request（doFilter 第一个参数）传入后由 TraceContext 零依赖捕获压测染色头（P1-1）
        return TraceContext.beginFilterRoot(request) ? Boolean.TRUE : null;
    }

    @Advice.OnMethodExit(onThrowable = Throwable.class)
    public static void exit(@Advice.Enter Object enter) {
        if (enter == Boolean.TRUE) {
            TraceContext.endFilterRoot();
        }
    }
}
