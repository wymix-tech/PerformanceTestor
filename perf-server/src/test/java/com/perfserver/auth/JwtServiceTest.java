package com.perfserver.auth;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

/**
 * JwtService 签发/校验单元测试。
 */
class JwtServiceTest {

    private AuthProperties properties;
    private JwtService jwtService;

    @BeforeEach
    void setUp() {
        properties = new AuthProperties();
        properties.setSecret("unit-test-secret-key-with-32-bytes-minimum!!");
        properties.setExpireHours(1);
        jwtService = new JwtService(properties);
    }

    @Test
    void issueAndVerifyRoundTrip() {
        String token = jwtService.issue("admin");
        assertNotNull(token);
        assertEquals("admin", jwtService.verify(token));
    }

    @Test
    void verifyRejectsTamperedToken() {
        String token = jwtService.issue("admin");
        String tampered = token.substring(0, token.length() - 4) + "XXXX";
        assertNull(jwtService.verify(tampered));
    }

    @Test
    void verifyRejectsGarbage() {
        assertNull(jwtService.verify("not-a-jwt"));
        assertNull(jwtService.verify(""));
        assertNull(jwtService.verify(null));
    }

    @Test
    void verifyRejectsExpiredToken() {
        properties.setExpireHours(-1); // 签发即过期
        String token = jwtService.issue("admin");
        assertNull(jwtService.verify(token));
    }

    @Test
    void verifyRejectsTokenSignedByDifferentKey() {
        AuthProperties other = new AuthProperties();
        other.setSecret("another-secret-key-with-different-content-32b!");
        other.setExpireHours(1);
        JwtService otherService = new JwtService(other);
        String token = otherService.issue("admin");
        assertNull(jwtService.verify(token));
    }
}
