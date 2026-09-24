package com.perfserver.health;

import com.perfserver.health.HealthCheckService.Probe;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;

/**
 * 启动自检：应用就绪后打印一次平台环境体检结论。
 *
 * <p>要解决的问题是"多机部署运维只能翻日志"——库连不上、磁盘写不了、
 * 建表脚本没执行，过去都要等第一个请求失败才发现。自检把这些前置到启动阶段，
 * 且每条失败都给可操作的下一步（跑哪个脚本、检查哪个环境变量）。
 *
 * <p><b>默认不阻断启动</b>：自检失败只告警。理由是多机部署下平台常先于依赖启动，
 * 一旦 fail-fast 会让"库还没起来"变成"平台起不来"的循环依赖。
 * 需要严格把关时置 {@code perf.health.fail-fast=true}。
 */
@Component
public class StartupSelfCheck implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(StartupSelfCheck.class);

    private final HealthCheckService healthCheckService;
    private final HealthProperties properties;

    public StartupSelfCheck(HealthCheckService healthCheckService, HealthProperties properties) {
        this.healthCheckService = healthCheckService;
        this.properties = properties;
    }

    @Override
    public void run(ApplicationArguments args) {
        if (!properties.isStartupCheckEnabled()) {
            log.info("startup self-check disabled (perf.health.startup-check-enabled=false)");
            return;
        }
        List<Probe> probes = probeAll();
        List<String> failures = new ArrayList<>();
        for (Probe probe : probes) {
            if (probe.ok()) {
                log.info("自检通过 | {}", probe.message());
            } else {
                log.warn("自检失败 | {}", probe.message());
                failures.add(probe.message());
            }
        }
        if (failures.isEmpty()) {
            log.info("平台启动自检全部通过（{} 项）", probes.size());
            return;
        }
        log.warn("平台启动自检发现 {} 项问题（已继续启动，确认是配置问题后请修复）：{}",
                failures.size(), String.join("；", failures));
        if (properties.isFailFast()) {
            throw new IllegalStateException("平台启动自检未通过（perf.health.fail-fast=true）: "
                    + String.join("；", failures));
        }
    }

    /** 全部探针（顺序执行，便于日志按"库 → 表 → 分区 → 存储 → 磁盘"阅读） */
    public List<Probe> probeAll() {
        return List.of(
                healthCheckService.checkDatabase(),
                healthCheckService.checkRequiredTables(),
                healthCheckService.checkPartitioning(),
                healthCheckService.checkStorageWritable(properties.getStorageDir()),
                healthCheckService.checkDisk(properties.getStorageDir()));
    }
}
