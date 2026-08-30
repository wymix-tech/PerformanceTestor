package com.perfserver.auth;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * Web 层鉴权配置：注册 JWT 拦截器。
 *
 * <p>白名单（免鉴权）：
 * <ul>
 *   <li>/api/auth/** —— 登录本身；</li>
 *   <li>/api/agent/** —— 探针上报通道（被测服务器无 JWT 能力，必须保持开放，
 *       平台侧依赖项目准入校验兜底）。</li>
 * </ul>
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
    }
}
