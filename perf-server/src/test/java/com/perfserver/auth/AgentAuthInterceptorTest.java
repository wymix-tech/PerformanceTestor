package com.perfserver.auth;

import jakarta.servlet.http.HttpServletResponse;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

import java.nio.charset.StandardCharsets;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * AgentAuthInterceptor 行为单元测试：令牌校验、空令牌放行兼容、鉴权开关。
 */
class AgentAuthInterceptorTest {

    private AuthProperties properties;
    private AgentAuthInterceptor interceptor;

    @BeforeEach
    void setUp() {
        properties = new AuthProperties();
        properties.setAgentToken("agent-token-abc");
        interceptor = new AgentAuthInterceptor(properties);
    }

    @Test
    void allowsMatchingToken() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader(AgentAuthInterceptor.TOKEN_HEADER, "agent-token-abc");
        assertTrue(interceptor.preHandle(request, response(), new Object()));
    }

    @Test
    void rejectsMissingTokenWith401() throws Exception {
        MockHttpServletResponse response = response();
        assertFalse(interceptor.preHandle(new MockHttpServletRequest(), response, new Object()));
        assertEquals(HttpServletResponse.SC_UNAUTHORIZED, response.getStatus());
        assertTrue(response.getContentAsString(StandardCharsets.UTF_8).contains("探针令牌无效"));
    }

    @Test
    void rejectsWrongToken() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader(AgentAuthInterceptor.TOKEN_HEADER, "wrong-token");
        assertFalse(interceptor.preHandle(request, response(), new Object()));
    }

    @Test
    void rejectsPrefixOnlyToken() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest();
        // 前缀相同但更长：常量时间比较下仍应拒绝（长度不同直接不等）
        request.addHeader(AgentAuthInterceptor.TOKEN_HEADER, "agent-token-abc-extra");
        assertFalse(interceptor.preHandle(request, response(), new Object()));
    }

    @Test
    void allowsAllWhenTokenNotConfigured() throws Exception {
        // 未配置令牌：保持开放，兼容存量 agent 与内网演示
        properties.setAgentToken("");
        assertTrue(interceptor.preHandle(new MockHttpServletRequest(), response(), new Object()));
    }

    @Test
    void allowsAllWhenAuthGloballyDisabled() throws Exception {
        // 全局鉴权关闭时探针通道同样放行（与 JwtAuthInterceptor 语义一致）
        properties.setEnabled(false);
        assertTrue(interceptor.preHandle(new MockHttpServletRequest(), response(), new Object()));
    }

    private static MockHttpServletResponse response() {
        MockHttpServletResponse response = new MockHttpServletResponse();
        response.setDefaultCharacterEncoding(StandardCharsets.UTF_8.name());
        return response;
    }
}
