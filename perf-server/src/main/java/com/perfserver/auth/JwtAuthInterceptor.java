package com.perfserver.auth;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.web.servlet.HandlerInterceptor;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * JWT 鉴权拦截器：校验 Authorization: Bearer &lt;token&gt;，并并行支持静态 API Token。
 *
 * <p>API Token（{@code perf.auth.api-token}）供 CI 流水线调用压测/门禁接口：
 * 命中即放行并把调用方标记为 CI（写入请求属性 {@link #CI_ATTR}），
 * 免去为流水线签发与轮换 JWT 的负担。留空则不启用。
 *
 * <p>未携带/非法/过期 token 一律返回 HTTP 401 + 统一 JSON 响应体，
 * 前端拦截器据此清除本地凭证并跳转登录页。
 */
public class JwtAuthInterceptor implements HandlerInterceptor {

    /** 请求属性：命中 API Token 时置为 true，供下游识别 CI 调用方 */
    public static final String CI_ATTR = "perf.auth.ci";

    private static final String API_TOKEN_HEADER = "X-Perf-Api-Token";

    private final AuthProperties properties;
    private final JwtService jwtService;

    public JwtAuthInterceptor(AuthProperties properties, JwtService jwtService) {
        this.properties = properties;
        this.jwtService = jwtService;
    }

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler)
            throws IOException {
        if (!properties.isEnabled()) {
            return true; // 鉴权关闭：直接放行
        }
        // CI 静态令牌：与 JWT 并行，命中即放行并标记调用方
        String apiToken = properties.getApiToken();
        if (apiToken != null && !apiToken.isBlank()) {
            String provided = request.getHeader(API_TOKEN_HEADER);
            if (provided != null && MessageDigest.isEqual(
                    apiToken.getBytes(StandardCharsets.UTF_8),
                    provided.getBytes(StandardCharsets.UTF_8))) {
                request.setAttribute(CI_ATTR, Boolean.TRUE);
                return true;
            }
        }
        String auth = request.getHeader("Authorization");
        if (auth != null && auth.startsWith("Bearer ")) {
            String subject = jwtService.verify(auth.substring(7));
            if (subject != null) {
                return true;
            }
        }
        response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
        response.setContentType("application/json;charset=UTF-8");
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.getWriter().write("{\"code\":401,\"message\":\"未登录或登录已过期\",\"data\":null}");
        return false;
    }
}
