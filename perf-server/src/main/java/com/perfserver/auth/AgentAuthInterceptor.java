package com.perfserver.auth;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.servlet.HandlerInterceptor;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 探针上报通道鉴权：校验 {@code X-Perf-Agent-Token} 请求头。
 *
 * <p>背景：{@code /api/agent/**} 是免 JWT 的（被测服务器上的 agent 无登录态），
 * 仅有项目准入校验兜底，内网任意请求都能伪造上报。本拦截器补上独立令牌校验。
 *
 * <p>设计取舍：
 * <ul>
 *   <li><b>不用 JWT</b>：agent 由被测服务器启动、无登录态，签发与轮换成本高收益低；
 *       独立静态令牌 + 可关闭开关是收益/复杂度最优解。</li>
 *   <li><b>令牌为空即放行</b>：保证内网演示环境与存量未升级的 agent 不中断
 *       （向后兼容，鉴权能力按需开启）。</li>
 *   <li><b>常量时间比较</b>：用 {@link MessageDigest#isEqual} 防时序侧信道。</li>
 * </ul>
 *
 * <p>令牌由 {@code perf.auth.agent-token}（环境变量 {@code PERF_AGENT_TOKEN}）配置，
 * agent 侧通过 {@code agentToken=<token>} 启动参数下发。
 */
public class AgentAuthInterceptor implements HandlerInterceptor {

    private static final Logger log = LoggerFactory.getLogger(AgentAuthInterceptor.class);

    /** 探针令牌请求头 */
    public static final String TOKEN_HEADER = "X-Perf-Agent-Token";

    /**
     * 鉴权失败计数（用于摘要日志降噪：伪造上报可能高频打来，逐条 ERROR 会刷爆日志）。
     */
    private final AtomicLong rejected = new AtomicLong();

    private final AuthProperties properties;

    public AgentAuthInterceptor(AuthProperties properties) {
        this.properties = properties;
    }

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler)
            throws IOException {
        String expected = properties.getAgentToken();
        // 未配置令牌：保持开放，兼容存量 agent 与内网演示
        if (expected == null || expected.isBlank() || !properties.isEnabled()) {
            return true;
        }
        String actual = request.getHeader(TOKEN_HEADER);
        if (actual != null && constantTimeEquals(expected, actual)) {
            return true;
        }
        long n = rejected.incrementAndGet();
        // 摘要降噪：前 3 条与之后每 100 条记一次，避免伪造上报刷爆日志
        if (n <= 3 || n % 100 == 0) {
            log.warn("探针上报鉴权失败（累计 {} 次）: uri={}, remote={}", n, request.getRequestURI(), request.getRemoteAddr());
        }
        response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
        response.setContentType("application/json;charset=UTF-8");
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.getWriter().write("{\"code\":401,\"message\":\"探针令牌无效\",\"data\":null}");
        return false;
    }

    /** 常量时间字符串比较，防时序侧信道 */
    private static boolean constantTimeEquals(String expected, String actual) {
        return MessageDigest.isEqual(
                expected.getBytes(StandardCharsets.UTF_8),
                actual.getBytes(StandardCharsets.UTF_8));
    }
}
