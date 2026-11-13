package com.perfserver.retention;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/**
 * 数据保留策略配置（perf.retention.*，见 application.yml）。
 *
 * <p>统一约定：任一保留天数为 {@code 0} 表示<b>不清理</b>该类数据（只建分区、不 DROP），
 * 便于首次上线先观察一轮再开启删除。
 *
 * <p>分区维护开关 {@code enabled} 默认开启（只建分区，安全）；
 * 删除动作由各保留天数控制，默认 30 天，把"建分区"与"删数据"解耦，避免上线即删历史。
 */
@Component
@ConfigurationProperties(prefix = "perf.retention")
public class RetentionProperties {

    /** 分区维护总开关（关闭后既不建分区也不清理） */
    private boolean enabled = true;

    /** 分区间隔小时数（定时任务周期，默认 1 小时；启动时总会先执行一次） */
    private int intervalHours = 1;

    /** 提前创建的未来分区天数（T+N，默认 2 天，覆盖周末/短假期不上线的窗口） */
    private int aheadDays = 2;

    /** 时序分区表保留天数（jvm_metric / stress_sample），0 = 不清理 */
    private int partitionDays = 30;

    /** 普通表保留天数（trace_chain），0 = 不清理；走分批 DELETE */
    private int traceChainDays = 30;

    /** 普通表保留天数（slow_sql），0 = 不清理；走分批 DELETE */
    private int slowSqlDays = 30;

    /** 普通表保留天数（slow_command），0 = 不清理；走分批 DELETE */
    private int slowCommandDays = 30;

    /** 单轮清理每批删除行数（防单事务过大阻塞写入） */
    private int deleteBatchSize = 5000;

    /** 单轮清理每个表最多删除批数（防单次扫描把 CPU/IO 吃满） */
    private int maxBatchesPerRun = 20;

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public int getIntervalHours() {
        return intervalHours;
    }

    public void setIntervalHours(int intervalHours) {
        this.intervalHours = intervalHours;
    }

    public int getAheadDays() {
        return aheadDays;
    }

    public void setAheadDays(int aheadDays) {
        this.aheadDays = aheadDays;
    }

    public int getPartitionDays() {
        return partitionDays;
    }

    public void setPartitionDays(int partitionDays) {
        this.partitionDays = partitionDays;
    }

    public int getTraceChainDays() {
        return traceChainDays;
    }

    public void setTraceChainDays(int traceChainDays) {
        this.traceChainDays = traceChainDays;
    }

    public int getSlowSqlDays() {
        return slowSqlDays;
    }

    public void setSlowSqlDays(int slowSqlDays) {
        this.slowSqlDays = slowSqlDays;
    }

    public int getSlowCommandDays() {
        return slowCommandDays;
    }

    public void setSlowCommandDays(int slowCommandDays) {
        this.slowCommandDays = slowCommandDays;
    }

    public int getDeleteBatchSize() {
        return deleteBatchSize;
    }

    public void setDeleteBatchSize(int deleteBatchSize) {
        this.deleteBatchSize = deleteBatchSize;
    }

    public int getMaxBatchesPerRun() {
        return maxBatchesPerRun;
    }

    public void setMaxBatchesPerRun(int maxBatchesPerRun) {
        this.maxBatchesPerRun = maxBatchesPerRun;
    }
}
