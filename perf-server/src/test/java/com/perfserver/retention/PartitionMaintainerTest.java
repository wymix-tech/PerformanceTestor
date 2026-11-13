package com.perfserver.retention;

import com.perfserver.mapper.RetentionMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 时序数据生命周期维护：分区名计算（含跨月/跨年）、保留期边界、分批清理、开关语义。
 *
 * <p>对应方案文档批次 1 P0-1 的验收标准。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class PartitionMaintainerTest {

    private static final DateTimeFormatter PARTITION_DATE = DateTimeFormatter.ofPattern("yyyyMMdd");

    @Mock
    private RetentionMapper mapper;

    private RetentionProperties properties;
    private PartitionMaintainer maintainer;

    @BeforeEach
    void setUp() {
        properties = new RetentionProperties();
        properties.setAheadDays(2);
        properties.setPartitionDays(30);
        properties.setTraceChainDays(0);
        properties.setSlowSqlDays(0);
        properties.setSlowCommandDays(0);
        properties.setDeleteBatchSize(5000);
        properties.setMaxBatchesPerRun(3);
        maintainer = new PartitionMaintainer(mapper, properties);
        when(mapper.deleteBefore(anyString(), anyString(), any(), anyInt())).thenReturn(0);
    }

    private Map<String, Object> partition(String name) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("partitionName", name);
        row.put("partitionDescription", "'2026-09-25 00:00:00'");
        return row;
    }

    private void stubPartitions(String table, List<String> names) {
        List<Map<String, Object>> rows = new ArrayList<>();
        for (String n : names) {
            rows.add(partition(n));
        }
        when(mapper.listPartitions(table)).thenReturn(rows);
    }

    // ==================== 分区名解析 ====================

    @Test
    void parsesStandardPartitionName() {
        assertEquals(LocalDate.of(2026, 9, 24), PartitionMaintainer.parsePartitionDate("p20260924"));
    }

    /** p_max 等非日期分区永不参与回收 */
    @Test
    void refusesToParseNonDatePartition() {
        assertNull(PartitionMaintainer.parsePartitionDate("p_max"));
        assertNull(PartitionMaintainer.parsePartitionDate("p2026"));
        assertNull(PartitionMaintainer.parsePartitionDate("partition_20260924"));
        assertNull(PartitionMaintainer.parsePartitionDate("p99999999"));
        assertNull(PartitionMaintainer.parsePartitionDate(null));
    }

    // ==================== 补分区 ====================

    /** 缺 T+1/T+2 分区时补齐，上界为次日零点 */
    @Test
    void createsMissingFuturePartitions() {
        LocalDate today = LocalDate.now();
        stubPartitions("jvm_metric", List.of("p_max"));
        stubPartitions("stress_sample", List.of("p_max"));

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        // 两张分区表 × 2 天 = 4 个新分区
        assertEquals(4, report.createdPartitions);
        String expected = "p" + today.plusDays(1).format(PARTITION_DATE);
        ArgumentCaptor<String> bound = ArgumentCaptor.forClass(String.class);
        verify(mapper, times(2)).addDailyPartition(anyString(), eq(expected), bound.capture());
        String firstBound = bound.getAllValues().get(0);
        LocalDateTime parsed = LocalDateTime.parse(firstBound, DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"));
        assertEquals(today.plusDays(2).atStartOfDay(), parsed);
    }

    /** 已有分区不重复创建（幂等） */
    @Test
    void skipsExistingPartitions() {
        LocalDate today = LocalDate.now();
        List<String> all = List.of("p_max",
                "p" + today.plusDays(1).format(PARTITION_DATE),
                "p" + today.plusDays(2).format(PARTITION_DATE));
        stubPartitions("jvm_metric", all);
        stubPartitions("stress_sample", all);

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        assertEquals(0, report.createdPartitions);
        verify(mapper, never()).addDailyPartition(anyString(), anyString(), anyString());
    }

    /** 未分区的表（information_schema 查不到分区）应跳过，不误发 DDL */
    @Test
    void skipsTableWithoutPartitions() {
        stubPartitions("jvm_metric", List.of());
        stubPartitions("stress_sample", List.of());

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        assertEquals(0, report.createdPartitions);
        verify(mapper, never()).addDailyPartition(anyString(), anyString(), anyString());
    }

    /** ADD PARTITION 失败（越过 MAXVALUE）时退化为 REORGANIZE p_max */
    @Test
    void fallsBackToReorganizeWhenAddRejected() {
        stubPartitions("jvm_metric", List.of("p_max"));
        stubPartitions("stress_sample", List.of());
        org.mockito.Mockito.doThrow(new RuntimeException("MAXVALUE"))
                .when(mapper).addDailyPartition(eq("jvm_metric"), anyString(), anyString());

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        assertEquals(2, report.createdPartitions);
        verify(mapper, times(2)).reorganizeMaxPartition(eq("jvm_metric"), anyString(), anyString());
    }

    /** 无 p_max 兜底分区时 ADD 失败直接上抛（由外层 try/catch 记录，不影响其他表） */
    @Test
    void noMaxPartitionDoesNotReorganize() {
        stubPartitions("jvm_metric", List.of("p20260901"));
        stubPartitions("stress_sample", List.of());
        org.mockito.Mockito.doThrow(new RuntimeException("add failed"))
                .when(mapper).addDailyPartition(eq("jvm_metric"), anyString(), anyString());

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        assertEquals(0, report.createdPartitions);
        verify(mapper, never()).reorganizeMaxPartition(anyString(), anyString(), anyString());
    }

    // ==================== 删除过期分区 ====================

    /** 早于保留期的分区整分区 DROP */
    @Test
    void dropsPartitionsOlderThanRetention() {
        LocalDate today = LocalDate.now();
        String expired = "p" + today.minusDays(31).format(PARTITION_DATE);
        String fresh = "p" + today.minusDays(10).format(PARTITION_DATE);
        stubPartitions("jvm_metric", List.of(expired, fresh, "p_max"));
        stubPartitions("stress_sample", List.of());

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        assertEquals(1, report.droppedPartitions);
        verify(mapper).dropPartition("jvm_metric", expired);
        verify(mapper, never()).dropPartition("jvm_metric", fresh);
        verify(mapper, never()).dropPartition("jvm_metric", PartitionMaintainer.MAX_PARTITION);
    }

    /**
     * 保留期边界（关键）：「保留 30 天」= 保留最近 30 个完整日期 = 今天起往前数 30 天。
     * 第 30 天的分区其下界落在窗口内（早于 today-29d 为零点，但该日 00:00 起仍在窗口内），
     * 故不能整分区 DROP——只有第 31 天（整分区数据都过期）才可删。
     * 这是「按天保留」最容易踩坑的一处，特此锁定。
     */
    @Test
    void keepsPartitionExactlyOnCutoffBoundary() {
        LocalDate today = LocalDate.now();
        // 第 30 天：仍保留
        String boundary = "p" + today.minusDays(29).format(PARTITION_DATE);
        // 第 31 天：可删
        String expired = "p" + today.minusDays(30).format(PARTITION_DATE);
        stubPartitions("jvm_metric", List.of(expired, boundary, "p_max"));
        stubPartitions("stress_sample", List.of());

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        assertEquals(1, report.droppedPartitions);
        verify(mapper).dropPartition("jvm_metric", expired);
        verify(mapper, never()).dropPartition("jvm_metric", boundary);
    }

    /** 保留期配置为 0：不清理任何分区（首次上线先只建分区观察） */
    @Test
    void zeroRetentionDisablesDrop() {
        properties.setPartitionDays(0);
        LocalDate today = LocalDate.now();
        stubPartitions("jvm_metric", List.of("p" + today.minusDays(999).format(PARTITION_DATE), "p_max"));
        stubPartitions("stress_sample", List.of());

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        assertEquals(0, report.droppedPartitions);
        verify(mapper, never()).dropPartition(anyString(), anyString());
        // 但补分区仍然执行（建分区安全，不依赖保留期）
        assertTrue(report.createdPartitions > 0);
    }

    // ==================== 普通表分批清理 ====================

    /** 每批跑满则继续下一批，最多 maxBatchesPerRun 批 */
    @Test
    void deletesPlainTablesInBatches() {
        properties.setTraceChainDays(30);
        stubPartitions("jvm_metric", List.of());
        stubPartitions("stress_sample", List.of());
        when(mapper.deleteBefore(eq("trace_chain"), eq("start_time"), any(), eq(5000))).thenReturn(5000);

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        verify(mapper, times(3)).deleteBefore(eq("trace_chain"), eq("start_time"), any(), anyInt());
        assertEquals(15000, report.deletedRows.get("trace_chain"));
    }

    /** 某批未跑满说明已删完，立即停止循环（避免空转） */
    @Test
    void stopsWhenBatchNotFull() {
        properties.setTraceChainDays(30);
        stubPartitions("jvm_metric", List.of());
        stubPartitions("stress_sample", List.of());
        when(mapper.deleteBefore(eq("trace_chain"), anyString(), any(), anyInt())).thenReturn(12);

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        verify(mapper, times(1)).deleteBefore(eq("trace_chain"), anyString(), any(), anyInt());
        assertEquals(12, report.deletedRows.get("trace_chain"));
    }

    /** 无数据可删时不记录该表（报告只保留非空动作） */
    @Test
    void noDeleteWhenNothingExpired() {
        properties.setTraceChainDays(30);
        stubPartitions("jvm_metric", List.of());
        stubPartitions("stress_sample", List.of());
        when(mapper.deleteBefore(anyString(), anyString(), any(), anyInt())).thenReturn(0);

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        assertTrue(report.deletedRows.isEmpty());
    }

    /** 保留天数为 0 的普通表完全不触发 DELETE */
    @Test
    void zeroRetentionSkipsPlainTable() {
        stubPartitions("jvm_metric", List.of());
        stubPartitions("stress_sample", List.of());

        maintainer.runOnce();

        verify(mapper, never()).deleteBefore(anyString(), anyString(), any(), anyInt());
    }

    /** 单表异常不阻断其余表（尽力而为） */
    @Test
    void singleTableFailureDoesNotAbortRun() {
        properties.setTraceChainDays(30);
        properties.setSlowSqlDays(30);
        stubPartitions("jvm_metric", List.of());
        stubPartitions("stress_sample", List.of());
        when(mapper.deleteBefore(eq("trace_chain"), anyString(), any(), anyInt()))
                .thenThrow(new RuntimeException("lock wait timeout"));
        when(mapper.deleteBefore(eq("slow_sql"), anyString(), any(), anyInt())).thenReturn(7);

        PartitionMaintainer.RetentionReport report = maintainer.runOnce();

        assertEquals(7, report.deletedRows.get("slow_sql"));
        assertNull(report.deletedRows.get("trace_chain"));
    }
}
