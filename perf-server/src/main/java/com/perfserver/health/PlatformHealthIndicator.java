package com.perfserver.health;

import com.perfserver.health.HealthCheckService.Probe;
import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.stereotype.Component;

/**
 * 平台健康指标：数据库连通、源码存储目录可写、磁盘剩余空间。
 *
 * <p>暴露为 {@code /actuator/health} 的 {@code platform} 细节项。
 * 判定口径与启动自检共用 {@link HealthCheckService}——线上看到的 DOWN
 * 应与启动时打印的告警一一对应，避免两套口径互相打架。
 *
 * <p>AI 连通性不进健康检查：无 key 时属正常部署形态（自动降级规则模式），
 * 把它算作 DOWN 会让健康检查常态化误报。
 */
@Component("platform")
public class PlatformHealthIndicator implements HealthIndicator {

    private final HealthCheckService healthCheckService;
    private final HealthProperties properties;

    public PlatformHealthIndicator(HealthCheckService healthCheckService, HealthProperties properties) {
        this.healthCheckService = healthCheckService;
        this.properties = properties;
    }

    @Override
    public Health health() {
        Probe db = healthCheckService.checkDatabase();
        Probe disk = healthCheckService.checkDisk(properties.getStorageDir());

        Health.Builder builder = db.ok() && disk.ok() ? Health.up() : Health.down();
        builder.withDetail("database", db.message());
        builder.withDetail("storage", disk.message());
        return builder.build();
    }
}
