package com.perfserver.auth;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * Web 层鉴权配置：注册 JWT 拦截器与探针上报拦截器。
 *
 * <p>JWT 拦截器（{@link JwtAuthInterceptor}）白名单（免鉴权）：
 * <ul>
 *   <li>/api/auth/** —— 登录本身；</li>
 *   <li>/api/agent/** —— 探针上报通道（被测服务器无 JWT 能力，必须保持开放）。</li>
 * </ul>
 *
 * <p>探针上报通道改由 {@link AgentAuthInterceptor} 独立把关：令牌配置后
 * 需携带 {@code X-Perf-Agent-Token}，未配置时放行以保持向后兼容。
 * 两个拦截器的路径互斥，不存在放行冲突。
 */
@Configuration
public class WebAuthConfig implements WebMvcConfigurer {

    private final AuthProperties properties;
    private final JwtService jwtService;

    public WebAuthConfig(AuthProperties properties, JwtService jwtService) {
        this.properties = properties;
        this.jwtService = jwtService;
    }

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(new JwtAuthInterceptor(properties, jwtService))
                .addPathPatterns("/api/**")
                .excludePathPatterns("/api/auth/**", "/api/agent/**");
        // 探针上报通道：独立令牌校验（免 JWT，但不再完全开放）
        registry.addInterceptor(new AgentAuthInterceptor(properties))
                .addPathPatterns("/api/agent/**");
    }
}
