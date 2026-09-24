package com.perfserver.auth;

import com.perfserver.common.ApiResponse;
import com.perfserver.common.TestMessages;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

/**
 * AuthController 登录逻辑单元测试。
 */
class AuthControllerTest {

    private AuthProperties properties;
    private AuthController controller;

    @BeforeEach
    void setUp() {
        properties = new AuthProperties();
        properties.setUsername("admin");
        properties.setPassword("secret-pass");
        properties.setSecret("unit-test-secret-key-with-32-bytes-minimum!!");
        controller = new AuthController(properties, new JwtService(properties), TestMessages.zh());
    }

    @Test
    @SuppressWarnings("unchecked")
    void loginSuccessReturnsToken() {
        ApiResponse<Map<String, Object>> res =
                controller.login(new AuthController.LoginRequest("admin", "secret-pass"));
        assertEquals(ApiResponse.CODE_OK, res.getCode());
        assertNotNull(res.getData().get("token"));
        assertEquals("admin", res.getData().get("username"));
    }

    @Test
    void loginRejectsWrongPassword() {
        ApiResponse<Map<String, Object>> res =
                controller.login(new AuthController.LoginRequest("admin", "wrong"));
        assertEquals(401, res.getCode());
        assertNull(res.getData());
        // P3-1：文案走 i18n，默认语言下为中文
        assertEquals("账号或密码错误", res.getMessage());
    }

    @Test
    void loginRejectsWrongUsername() {
        ApiResponse<Map<String, Object>> res =
                controller.login(new AuthController.LoginRequest("root", "secret-pass"));
        assertEquals(401, res.getCode());
    }

    @Test
    void loginRejectsBlankCredentials() {
        assertEquals(401, controller.login(new AuthController.LoginRequest("", "")).getCode());
        assertEquals(401, controller.login(null).getCode());
    }

    @Test
    void loginMessageFollowsRequestLocale() {
        // 通过 LocaleContextHolder 模拟 Accept-Language: en 的请求
        var prev = org.springframework.context.i18n.LocaleContextHolder.getLocale();
        try {
            org.springframework.context.i18n.LocaleContextHolder.setLocale(java.util.Locale.ENGLISH);
            AuthController en = new AuthController(properties, new JwtService(properties),
                    new com.perfserver.common.Messages(com.perfserver.common.TestMessages.source()));
            assertEquals("Invalid username or password",
                    en.login(new AuthController.LoginRequest("admin", "wrong")).getMessage());
        } finally {
            org.springframework.context.i18n.LocaleContextHolder.setLocale(prev);
        }
    }
}
