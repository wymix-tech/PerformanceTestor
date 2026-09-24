package com.perfserver.auth;

import com.perfserver.common.ApiResponse;
import com.perfserver.common.Messages;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 登录接口：POST /api/auth/login（白名单免鉴权）。
 *
 * <p>轻量单管理员模式：账号密码来自 perf.auth 配置（支持环境变量注入）。
 * 登录失败统一返回 401 业务码，不区分账号/密码错误以防枚举。
 */
@RestController
@RequestMapping("/api/auth")
public class AuthController {

    private static final Logger log = LoggerFactory.getLogger(AuthController.class);

    private final AuthProperties properties;
    private final JwtService jwtService;
    private final Messages messages;

    public AuthController(AuthProperties properties, JwtService jwtService, Messages messages) {
        this.properties = properties;
        this.jwtService = jwtService;
        this.messages = messages;
    }

    /** 登录请求体 */
    public record LoginRequest(String username, String password) {
    }

    @PostMapping("/login")
    public ApiResponse<Map<String, Object>> login(@RequestBody LoginRequest req) {
        if (req == null || isBlank(req.username()) || isBlank(req.password())) {
            return ApiResponse.error(401, messages.get("auth.missing.credentials"));
        }
        boolean ok = properties.getUsername().equals(req.username())
                && properties.getPassword().equals(req.password());
        if (!ok) {
            log.warn("login failed for username: {}", req.username());
            return ApiResponse.error(401, messages.get("auth.invalid.credentials"));
        }
        String token = jwtService.issue(properties.getUsername());
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("token", token);
        data.put("username", properties.getUsername());
        data.put("expireHours", properties.getExpireHours());
        return ApiResponse.ok(data);
    }

    /** 校验当前 token 是否有效（前端可用于会话探测） */
    @GetMapping("/me")
    public ApiResponse<Map<String, Object>> me() {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("username", properties.getUsername());
        data.put("authEnabled", properties.isEnabled());
        return ApiResponse.ok(data);
    }

    private static boolean isBlank(String s) {
        return s == null || s.isBlank();
    }
}
