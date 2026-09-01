package com.perfserver.auth;

import jakarta.servlet.http.HttpServletResponse;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

import java.nio.charset.StandardCharsets;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * JwtAuthInterceptor 行为单元测试（含鉴权开关与 401 响应体）。
 */
class JwtAuthInterceptorTest {

    private AuthProperties properties;
    private JwtService jwtService;
    private JwtAuthInterceptor interceptor;

    @BeforeEach
    void setUp() {
        properties = new AuthProperties();
        properties.setSecret("unit-test-secret-key-with-32-bytes-minimum!!");
        properties.setExpireHours(1);
        jwtService = new JwtService(properties);
        interceptor = new JwtAuthInterceptor(properties, jwtService);
    }

    @Test
    void allowsValidBearerToken() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader("Authorization", "Bearer " + jwtService.issue("admin"));
        assertTrue(interceptor.preHandle(request, response(), new Object()));
    }

    @Test
    void rejectsMissingTokenWith401() throws Exception {
        MockHttpServletResponse response = response();
        assertFalse(interceptor.preHandle(new MockHttpServletRequest(), response, new Object()));
        assertEquals(HttpServletResponse.SC_UNAUTHORIZED, response.getStatus());
        assertTrue(response.getContentAsString(StandardCharsets.UTF_8).contains("未登录"));
    }

    @Test
    void rejectsInvalidTokenWith401() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader("Authorization", "Bearer invalid-token-value");
        MockHttpServletResponse response = response();
        assertFalse(interceptor.preHandle(request, response, new Object()));
        assertEquals(HttpServletResponse.SC_UNAUTHORIZED, response.getStatus());
    }

    @Test
    void rejectsNonBearerScheme() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader("Authorization", "Basic dXNlcjpwYXNz");
        assertFalse(interceptor.preHandle(request, response(), new Object()));
    }

    @Test
    void passesThroughWhenAuthDisabled() throws Exception {
        properties.setEnabled(false);
        assertTrue(interceptor.preHandle(new MockHttpServletRequest(), response(), new Object()));
    }

    @Test
    void allowsStaticApiTokenAndMarksCiCaller() throws Exception {
        properties.setApiToken("ci-token-xyz");
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader("X-Perf-Api-Token", "ci-token-xyz");
        assertTrue(interceptor.preHandle(request, response(), new Object()));
        assertEquals(Boolean.TRUE, request.getAttribute(JwtAuthInterceptor.CI_ATTR),
                "命中 API Token 应标记调用方为 CI");
    }

    @Test
    void rejectsWrongApiToken() throws Exception {
        properties.setApiToken("ci-token-xyz");
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader("X-Perf-Api-Token", "wrong");
        MockHttpServletResponse response = response();
        assertFalse(interceptor.preHandle(request, response, new Object()));
        assertEquals(HttpServletResponse.SC_UNAUTHORIZED, response.getStatus());
    }

    @Test
    void apiTokenIsIgnoredWhenNotConfigured() throws Exception {
        // 未配置 API Token：不得因请求头带值而放行
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader("X-Perf-Api-Token", "anything");
        assertFalse(interceptor.preHandle(request, response(), new Object()));
    }

    @Test
    void jwtPathDoesNotMarkCiCaller() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader("Authorization", "Bearer " + jwtService.issue("admin"));
        assertTrue(interceptor.preHandle(request, response(), new Object()));
        assertNull(request.getAttribute(JwtAuthInterceptor.CI_ATTR), "JWT 登录不应被标记为 CI 调用");
    }

    private static MockHttpServletResponse response() {
        MockHttpServletResponse response = new MockHttpServletResponse();
        response.setDefaultCharacterEncoding(StandardCharsets.UTF_8.name());
        return response;
    }
}
