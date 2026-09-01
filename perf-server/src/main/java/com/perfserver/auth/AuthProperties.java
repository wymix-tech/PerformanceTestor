package com.perfserver.auth;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/**
 * 轻量鉴权配置（perf.auth.*，见 application.yml）。
 *
 * <p>管理员账号与 JWT 密钥支持环境变量覆盖（PERF_ADMIN_USER / PERF_ADMIN_PASSWORD / PERF_JWT_SECRET），
 * 避免敏感信息硬编码入库。
 */
@Component
@ConfigurationProperties(prefix = "perf.auth")
public class AuthProperties {

    /** 是否启用鉴权（关闭时拦截器直接放行，便于本地调试） */
    private boolean enabled = true;

    /** 管理员账号 */
    private String username = "admin";

    /** 管理员密码 */
    private String password = "admin123";

    /** HS256 签名密钥（至少 32 字节） */
    private String secret = "perf-testor-default-jwt-secret-please-change-in-production";

    /** token 有效期（小时） */
    private long expireHours = 12;

    /**
     * 探针上报令牌（PERF_AGENT_TOKEN）：配置后 /api/agent/** 需携带 X-Perf-Agent-Token。
     * 留空 = 不校验，保持开放（兼容存量 agent 与内网演示）。
     */
    private String agentToken = "";

    /**
     * 静态 API Token（PERF_API_TOKEN）：供 CI 流水线调用压测/门禁接口。
     * 与 JWT 并行生效，命中即放行并标记调用方为 CI。留空 = 不启用。
     */
    private String apiToken = "";

    public String getAgentToken() {
        return agentToken;
    }

    public void setAgentToken(String agentToken) {
        this.agentToken = agentToken;
    }

    public String getApiToken() {
        return apiToken;
    }

    public void setApiToken(String apiToken) {
        this.apiToken = apiToken;
    }

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public String getUsername() {
        return username;
    }

    public void setUsername(String username) {
        this.username = username;
    }

    public String getPassword() {
        return password;
    }

    public void setPassword(String password) {
        this.password = password;
    }

    public String getSecret() {
        return secret;
    }

    public void setSecret(String secret) {
        this.secret = secret;
    }

    public long getExpireHours() {
        return expireHours;
    }

    public void setExpireHours(long expireHours) {
        this.expireHours = expireHours;
    }
}
