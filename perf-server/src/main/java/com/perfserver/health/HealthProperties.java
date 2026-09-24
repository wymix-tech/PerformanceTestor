package com.perfserver.health;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/**
 * 健康检查与启动自检配置（perf.health.*，见 application.yml）。
 *
 * <p>拆成独立配置类而非复用 {@code perf.source.storage-dir} 等散落属性：
 * 自检要同时读源码目录、磁盘阈值、必需表清单，散读会让"自检开关"无法统一关闭。
 */
@Component
@ConfigurationProperties(prefix = "perf.health")
public class HealthProperties {

    /** 启动自检总开关（默认开启；失败只告警不阻断启动） */
    private boolean startupCheckEnabled = true;

    /** 严格模式：自检不通过时直接终止启动（默认关闭，便于首次部署排障） */
    private boolean failFast = false;

    /** 磁盘剩余空间告警阈值（MB），低于该值健康检查返回 DOWN */
    private int minFreeDiskMb = 512;

    /** 源码文件存储根目录（与 {@code perf.source.storage-dir} 保持一致，自检其可写性） */
    private String storageDir = "/data/perf-source";

    /** 启动自检的必需表清单（默认取核心表；缺失即提示初始化脚本未执行） */
    private String[] requiredTables = {
            "app_instance", "jvm_metric", "trace_chain", "slow_sql", "slow_command",
            "stress_task", "stress_sample", "source_repo", "source_file",
            "performance_issue", "fix_suggestion", "ai_knowledge"};

    /** 分区维护最低 MySQL 版本（低于该版本分区表管理语义不可靠） */
    private String minMysqlVersion = "5.7";

    public boolean isStartupCheckEnabled() {
        return startupCheckEnabled;
    }

    public void setStartupCheckEnabled(boolean startupCheckEnabled) {
        this.startupCheckEnabled = startupCheckEnabled;
    }

    public boolean isFailFast() {
        return failFast;
    }

    public void setFailFast(boolean failFast) {
        this.failFast = failFast;
    }

    public int getMinFreeDiskMb() {
        return minFreeDiskMb;
    }

    public void setMinFreeDiskMb(int minFreeDiskMb) {
        this.minFreeDiskMb = minFreeDiskMb;
    }

    public String getStorageDir() {
        return storageDir;
    }

    public void setStorageDir(String storageDir) {
        this.storageDir = storageDir;
    }

    public String[] getRequiredTables() {
        return requiredTables;
    }

    public void setRequiredTables(String[] requiredTables) {
        this.requiredTables = requiredTables;
    }

    public String getMinMysqlVersion() {
        return minMysqlVersion;
    }

    public void setMinMysqlVersion(String minMysqlVersion) {
        this.minMysqlVersion = minMysqlVersion;
    }
}
