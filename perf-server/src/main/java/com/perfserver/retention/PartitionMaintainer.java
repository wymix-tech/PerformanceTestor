package com.perfserver.retention;

import com.perfserver.mapper.RetentionMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * 时序数据生命周期维护：为分区表补齐未来日分区 + 按保留期回收历史数据。
 *
 * <p><b>要解决的问题</b>：建表脚本里 {@code jvm_metric} / {@code stress_sample} 已按
 * {@code RANGE COLUMNS} 分区，但仓库内零调用 {@code create_daily_partition}，
 * 也没有任何保留策略——数据全部落进 {@code p_max}，分区退化为普通表，
 * 时间久了单表无限增长、查询与清理都只能全表扫。
 *
 * <p><b>做法</b>：
 * <ol>
 *   <li><b>补分区</b>：查 {@code information_schema.PARTITIONS} 现有日分区，
 *       为未来 T+1..T+{@code aheadDays} 逐个补齐 {@code pYYYYMMDD}；
 *       若仍有 {@code p_max} 兜底分区则一并保留（数据不会因缺分区而写入失败）。</li>
 *   <li><b>删除过期分区</b>：按分区名解析日期，早于保留期的整分区 {@code DROP}——
 *       比逐行 DELETE 快几个数量级，且不产生大事务与 undo 膨胀。</li>
 *   <li><b>普通表分批清理</b>：{@code trace_chain} / {@code slow_sql} / {@code slow_command}
 *       无分区，用 {@code DELETE ... LIMIT} 分批循环，每轮限量，避免长事务阻塞上报写入。</li>
 * </ol>
 *
 * <p><b>安全性</b>：全部操作 try/catch 且只告警不抛出，不影响主流程；
 * 表名/列名来自代码内白名单常量，分区名经 {@code ^p\d{8}$} 校验；
 * 保留天数为 0 表示该类数据不清理（首次上线建议先只建分区、观察一轮再开删）。
 */
@Service
public class PartitionMaintainer {

    private static final Logger log = LoggerFactory.getLogger(PartitionMaintainer.class);

    /** 分区名格式：p + 8 位日期（20260924） */
    static final Pattern PARTITION_NAME = Pattern.compile("^p\\d{8}$");
    private static final DateTimeFormatter PARTITION_DATE = DateTimeFormatter.ofPattern("yyyyMMdd");
    private static final DateTimeFormatter PARTITION_BOUND = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    /** 兜底分区名（建表脚本中的 MAXVALUE 分区） */
    static final String MAX_PARTITION = "p_max";

    /**
     * 分区表白名单：表名 → 分区列。
     * 用代码常量而非配置项，避免配置被改坏后拼进 DDL（DDL 不支持参数化，标识符只能拼串）。
     *
     * <p>public 供启动自检/健康检查复用：自检要按"建表脚本声明的分区表"回查
     * {@code information_schema}，与本类维护的表必须是同一份清单，否则会出现
     * "自检说分区就绪、维护却漏了某张表"的分裂。
     */
    public static final Map<String, String> PARTITIONED_TABLES = Map.of(
            "jvm_metric", "collect_time",
            "stress_sample", "sample_time");

    /**
     * 普通表（非分区）清理白名单：表名 → 时间列 → 配置里的保留天数取值器。
     * 同样以常量约束表/列名，杜绝外部输入进入 SQL 标识符位置。
     */
    private static final List<PlainTable> PLAIN_TABLES = List.of(
            new PlainTable("trace_chain", "start_time"),
            new PlainTable("slow_sql", "execute_time"),
            new PlainTable("slow_command", "execute_time"));

    private final RetentionMapper mapper;
    private final RetentionProperties properties;

    public PartitionMaintainer(RetentionMapper mapper, RetentionProperties properties) {
        this.mapper = mapper;
        this.properties = properties;
    }

    /** 启动就绪后立即执行一次：新建项目不必等到下一个整点才有分区 */
    @EventListener(ApplicationReadyEvent.class)
    public void onReady() {
        if (!properties.isEnabled()) {
            log.info("retention: 分区维护已关闭（perf.retention.enabled=false）");
            return;
        }
        // 异步化避免拖慢启动：DB 未就绪时下一轮定时任务会补上
        Thread t = new Thread(this::runSafely, "retention-init");
        t.setDaemon(true);
        t.start();
    }

    /**
     * 定时维护（默认 1 小时一次）。
     *
     * <p>{@code fixedDelay} 而非 {@code fixedRate}：单轮耗时随数据量变化，
     * 用「上一轮结束后再等 N 小时」保证不会任务堆积。
     *
     * <p>注意：{@code initialDelayString} 只接受毫秒数值（或 PT 前缀 ISO-8601），
     * 不能写 "60s" 这种简单时长写法——写错不会在单测暴露，而是直接导致
     * Spring 上下文启动失败（BeanCreationException: invalid initialDelayString）。
     * 周期侧用 SpEL 把 interval-hours（小时）换算为毫秒，保持原有配置语义。
     */
    @Scheduled(fixedDelayString = "#{${perf.retention.interval-hours:1} * 3600000}", initialDelayString = "60000")
    public void scheduled() {
        if (!properties.isEnabled()) {
            return;
        }
        runSafely();
    }

    /** 单轮维护入口（吞掉全部异常，保证调度不被中断） */
    public void runSafely() {
        try {
            runOnce();
        } catch (Exception e) {
            log.warn("retention: 本轮维护失败（不影响主流程）", e);
        }
    }

    /**
     * 执行一轮：补分区 → 删过期分区 → 普通表分批清理。
     *
     * @return 本轮动作摘要（供日志与单测断言）
     */
    public RetentionReport runOnce() {
        RetentionReport report = new RetentionReport();
        LocalDate today = LocalDate.now();

        for (Map.Entry<String, String> entry : PARTITIONED_TABLES.entrySet()) {
            String table = entry.getKey();
            try {
                maintainPartitions(table, today, report);
            } catch (Exception e) {
                // 单表失败不影响其他表
                log.warn("retention: 分区维护失败, table={}", table, e);
            }
        }

        for (PlainTable table : PLAIN_TABLES) {
            int days = retentionDays(table.name());
            if (days <= 0) {
                continue;
            }
            try {
                deleteInBatches(table, days, report);
            } catch (Exception e) {
                log.warn("retention: 分批清理失败, table={}", table.name(), e);
            }
        }

        if (!report.isEmpty()) {
            log.info("retention: 本轮维护完成 - 新建分区 {}, 删除分区 {}, 清理行数 {}",
                    report.createdPartitions, report.droppedPartitions, report.deletedRows);
        }
        return report;
    }

    /** 分区维护：补齐未来分区 + 删除超出保留期的日分区 */
    private void maintainPartitions(String table, LocalDate today, RetentionReport report) {
        List<Map<String, Object>> rows = mapper.listPartitions(table);
        Set<String> existing = new java.util.HashSet<>();
        for (Map<String, Object> row : rows) {
            Object name = row.get("partitionName");
            if (name != null) {
                existing.add(String.valueOf(name));
            }
        }
        if (existing.isEmpty()) {
            // 表不存在或未分区：跳过（建表脚本负责分区表结构）
            return;
        }
        boolean hasMax = existing.contains(MAX_PARTITION);

        // 1) 补齐未来分区：p20260925 的上界是 2026-09-26 00:00:00
        for (int offset = 1; offset <= Math.max(1, properties.getAheadDays()); offset++) {
            LocalDate day = today.plusDays(offset);
            String partition = "p" + day.format(PARTITION_DATE);
            if (existing.contains(partition)) {
                continue;
            }
            LocalDateTime bound = day.plusDays(1).atStartOfDay();
            createPartition(table, partition, bound, hasMax);
            existing.add(partition);
            report.createdPartitions++;
            log.info("retention: 新建分区 {}.{}（上界 {}）", table, partition, bound);
        }

        // 2) 删除过期分区：只删"整分区数据都已过期"的分区
        int keepDays = properties.getPartitionDays();
        if (keepDays <= 0) {
            return;
        }
        // 保留窗口为 [今天-(keepDays-1), 今天]，最早该保留的日期即 keepDays-1 天前。
        // 例：keepDays=30 → 保留最近 30 天（今天 + 前 29 天），31 天前的分区才可删。
        // 严格早于该日期的分区，其全部时间点都已超出保留窗口 → 可整分区 DROP；
        // 恰好等于该日期的分区仍有部分时刻在窗口内 → 必须保留（边界取"早于"而非"小于等于"）。
        LocalDate earliestKept = today.minusDays(Math.max(0, keepDays - 1L));
        for (String partition : sortedPartitions(existing)) {
            LocalDate day = parsePartitionDate(partition);
            if (day == null || !day.isBefore(earliestKept)) {
                continue;
            }
            mapper.dropPartition(table, partition);
            report.droppedPartitions++;
            log.info("retention: 删除过期分区 {}.{}（分区日期 {} 早于保留窗口起点 {}）",
                    table, partition, day, earliestKept);
        }
    }

    /**
     * 新建分区：优先 {@code ADD PARTITION}（上界必小于 MAXVALUE 时合法）。
     * 若剩余 MAXVALUE 只有 p_max 且新分区上界会越过它，则退化为
     * {@code REORGANIZE p_max INTO (新分区, p_max)}——部分 MySQL 版本不允许
     * 在 MAXVALUE 之后 ADD，统一走 REORGANIZE 更稳，但会重建 p_max（可能搬数据）。
     * 故先试 ADD，失败再 REORGANIZE。
     */
    private void createPartition(String table, String partition, LocalDateTime bound, boolean hasMax) {
        String boundText = bound.format(PARTITION_BOUND);
        try {
            mapper.addDailyPartition(table, partition, boundText);
        } catch (Exception addFailed) {
            if (!hasMax) {
                throw addFailed;
            }
            // p_max 已存在时 ADD 会被拒（新分区上界大于 MAXVALUE 分区上界不允许）→ 拆分 p_max
            log.debug("retention: ADD PARTITION 失败，改用 REORGANIZE p_max, table={}, partition={}",
                    table, partition);
            mapper.reorganizeMaxPartition(table, partition, boundText);
        }
    }

    /** 普通表分批删除：每批最多 batchSize 行，最多 maxBatchesPerRun 批 */
    private void deleteInBatches(PlainTable table, int days, RetentionReport report) {
        LocalDateTime before = LocalDate.now().minusDays(days).atStartOfDay();
        int batchSize = Math.max(1, properties.getDeleteBatchSize());
        int maxBatches = Math.max(1, properties.getMaxBatchesPerRun());
        int total = 0;
        for (int i = 0; i < maxBatches; i++) {
            int deleted = mapper.deleteBefore(table.name(), table.timeColumn(), before, batchSize);
            total += deleted;
            if (deleted < batchSize) {
                // 本批未跑满说明已删完，无需再循环
                break;
            }
        }
        if (total > 0) {
            report.deletedRows.put(table.name(), total);
            log.info("retention: 清理 {} 中早于 {} 的数据 {} 行（保留 {} 天）",
                    table.name(), before, total, days);
        }
    }

    /** 各表保留天数（0 = 不清理） */
    private int retentionDays(String table) {
        return switch (table) {
            case "trace_chain" -> properties.getTraceChainDays();
            case "slow_sql" -> properties.getSlowSqlDays();
            case "slow_command" -> properties.getSlowCommandDays();
            default -> 0;
        };
    }

    /** 按分区名日期升序（删除顺序与建分区顺序一致，避免误删刚建的） */
    private static List<String> sortedPartitions(Set<String> partitions) {
        return partitions.stream()
                .filter(p -> parsePartitionDate(p) != null)
                .sorted()
                .toList();
    }

    /**
     * 解析分区名中的日期；非 {@code pYYYYMMDD} 形式（如 {@code p_max}）返回 null。
     *
     * <p>返回 null 的分区永不被删除——这是刻意的保守设计：
     * 手工建的非标准分区名（含 MAXVALUE 兜底分区）不参与保留期回收。
     */
    static LocalDate parsePartitionDate(String partition) {
        if (partition == null || !PARTITION_NAME.matcher(partition).matches()) {
            return null;
        }
        try {
            return LocalDate.parse(partition.substring(1), PARTITION_DATE);
        } catch (Exception e) {
            return null;
        }
    }

    /** 普通表白名单项 */
    private record PlainTable(String name, String timeColumn) {
    }

    /** 单轮维护摘要 */
    public static class RetentionReport {
        public int createdPartitions;
        public int droppedPartitions;
        public final Map<String, Integer> deletedRows = new java.util.LinkedHashMap<>();

        public boolean isEmpty() {
            return createdPartitions == 0 && droppedPartitions == 0 && deletedRows.isEmpty();
        }

        public int totalDeletedRows() {
            return deletedRows.values().stream().mapToInt(Integer::intValue).sum();
        }
    }
}
