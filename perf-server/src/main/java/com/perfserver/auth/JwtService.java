package com.perfserver.auth;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.springframework.stereotype.Service;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Date;

/**
 * JWT 签发与校验（HS256）。
 *
 * <p>仅承载用户名 subject，不携带角色等声明，保持轻量。
 * 密钥长度不足 32 字节时构造即失败（HMAC-SHA256 规范要求）。
 */
@Service
public class JwtService {

    private final AuthProperties properties;
    private volatile SecretKey key;

    public JwtService(AuthProperties properties) {
        this.properties = properties;
    }

    private SecretKey key() {
        SecretKey k = key;
        if (k == null) {
            synchronized (this) {
                if (key == null) {
                    byte[] bytes = properties.getSecret().getBytes(StandardCharsets.UTF_8);
                    if (bytes.length < 32) {
                        throw new IllegalStateException("perf.auth.secret 长度不足 32 字节，无法用于 HS256 签名");
                    }
                    key = Keys.hmacShaKeyFor(bytes);
                    k = key;
                }
            }
        }
        return k;
    }

    /** 为指定用户签发 token */
    public String issue(String username) {
        Instant now = Instant.now();
        return Jwts.builder()
                .subject(username)
                .issuedAt(Date.from(now))
                .expiration(Date.from(now.plus(properties.getExpireHours(), ChronoUnit.HOURS)))
                .signWith(key())
                .compact();
    }

    /**
     * 校验 token 并返回 subject。
     *
     * @return 合法时返回用户名；非法/过期返回 null（不抛异常，调用方按未登录处理）
     */
    public String verify(String token) {
        try {
            Claims claims = Jwts.parser().verifyWith(key()).build()
                    .parseSignedClaims(token).getPayload();
            return claims.getSubject();
        } catch (JwtException | IllegalArgumentException e) {
            return null;
        }
    }
}
