package com.perfserver.auth;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.web.servlet.HandlerInterceptor;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

/**
 * JWT 鉴权拦截器：校验 Authorization: Bearer &lt;token&gt;。
 *
 * <p>未携带/非法/过期 token 一律返回 HTTP 401 + 统一 JSON 响应体，
 * 前端拦截器据此清除本地凭证并跳转登录页。
 */
public class JwtAuthInterceptor implements HandlerInterceptor {

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
