package com.perfserver.health;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.io.TempDir;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.when;

/**
 * 平台自检：库连通/版本门槛、必需表、分区有效性、目录可写与磁盘余量。
 *
 * <p>重点在"失败必须给出可操作指引"与"不能把正常部署判成失败"
 * （例如非 MySQL 版本串不应因版本解析失败而误报）。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class HealthCheckServiceTest {

    @Mock
    private HealthMapper mapper;

    private HealthProperties properties;
    private HealthCheckService service;

    @BeforeEach
    void setUp() {
        properties = new HealthProperties();
        properties.setMinFreeDiskMb(1);
        properties.setMinMysqlVersion("5.7");
        service = new HealthCheckService(mapper, properties);
    }

    // ==================== 数据库 ====================

    @Test
    void databaseOkReportsVersion() {
        when(mapper.ping()).thenReturn(1);
        when(mapper.databaseVersion()).thenReturn("8.0.36 / MySQL Community Server");

        HealthCheckService.Probe probe = service.checkDatabase();

        assertTrue(probe.ok());
        assertTrue(probe.message().contains("8.0.36"));
    }

    /** 库不可达：信息里必须带上排查用的环境变量名 */
    @Test
    void databaseDownGivesActionableHint() {
        when(mapper.ping()).thenThrow(new RuntimeException("Communications link failure"));

        HealthCheckService.Probe probe = service.checkDatabase();

        assertFalse(probe.ok());
        assertTrue(probe.message().contains("MYSQL_URL"), "应提示检查连接配置，实际 " + probe.message());
    }

    /** 分区维护依赖 5.7+；低版本要判失败并说明原因 */
    @Test
    void mysqlTooOldFails() {
        when(mapper.ping()).thenReturn(1);
        when(mapper.databaseVersion()).thenReturn("5.6.51 / MySQL Community Server");

        HealthCheckService.Probe probe = service.checkDatabase();

        assertFalse(probe.ok());
        assertTrue(probe.message().contains("5.7"), "应说明最低版本要求，实际 " + probe.message());
    }

    @Test
    void mysqlAtThresholdPasses() {
        when(mapper.ping()).thenReturn(1);
        when(mapper.databaseVersion()).thenReturn("5.7.44 / MySQL Community Server");
        assertTrue(service.checkDatabase().ok());
    }

    /** 版本号必须按数值段比较：5.10 > 5.7，字典序会判反 */
    @Test
    void versionCompareIsNumeric() {
        assertTrue(HealthCheckService.compareVersion("5.10.1", "5.7") > 0);
        assertTrue(HealthCheckService.compareVersion("5.7", "5.7.0") == 0);
        assertTrue(HealthCheckService.compareVersion("8.0", "8.0.36") < 0);
    }

    /** 取不到版本（非 MySQL / 权限受限）不应判失败：探活已通过就是可用 */
    @Test
    void unknownVersionStillOk() {
        when(mapper.ping()).thenReturn(1);
        when(mapper.databaseVersion()).thenThrow(new RuntimeException("access denied"));

        HealthCheckService.Probe probe = service.checkDatabase();
        assertTrue(probe.ok());
    }

    @Test
    void pingWrongValueFails() {
        when(mapper.ping()).thenReturn(null);
        assertFalse(service.checkDatabase().ok());
    }

    // ==================== 必需表 ====================

    @Test
    void allRequiredTablesPresent() {
        properties.setRequiredTables(new String[]{"source_repo", "ai_knowledge"});
        when(mapper.findExistingTables(anyList())).thenReturn(List.of("source_repo", "ai_knowledge"));

        HealthCheckService.Probe probe = service.checkRequiredTables();
        assertTrue(probe.ok());
        verifyTablesChecked();
    }

    /** 缺表要把缺的名字列出来，并指向初始化脚本 */
    @Test
    void missingTablesListedWithScriptHint() {
        properties.setRequiredTables(new String[]{"source_repo", "ai_knowledge"});
        when(mapper.findExistingTables(anyList())).thenReturn(List.of("source_repo"));

        HealthCheckService.Probe probe = service.checkRequiredTables();

        assertFalse(probe.ok());
        assertTrue(probe.message().contains("ai_knowledge"));
        assertTrue(probe.message().contains("init.sql"), "应指向初始化脚本，实际 " + probe.message());
    }

    @Test
    void tableQueryFailureFails() {
        properties.setRequiredTables(new String[]{"source_repo"});
        when(mapper.findExistingTables(anyList())).thenThrow(new RuntimeException("no information_schema"));
        assertFalse(service.checkRequiredTables().ok());
    }

    @Test
    void emptyRequiredTablesSkips() {
        properties.setRequiredTables(new String[0]);
        assertTrue(service.checkRequiredTables().ok());
    }

    // ==================== 分区 ====================

    /** 分区表必须真的建了分区：未分区时数据全落 p_max，分区退化为普通表 */
    @Test
    void missingPartitioningFails() {
        when(mapper.findPartitionedTables(anyList())).thenReturn(List.of("jvm_metric"));

        HealthCheckService.Probe probe = service.checkPartitioning();

        assertFalse(probe.ok());
        assertTrue(probe.message().contains("stress_sample"), "应指出未分区的表，实际 " + probe.message());
    }

    @Test
    void partitioningReadyPasses() {
        when(mapper.findPartitionedTables(anyList())).thenReturn(List.of("jvm_metric", "stress_sample"));
        assertTrue(service.checkPartitioning().ok());
    }

    // ==================== 目录与磁盘 ====================

    @Test
    void writableDirPasses(@TempDir Path dir) {
        assertTrue(service.checkStorageWritable(dir.toString()).ok());
    }

    /** 目录不可创建（父路径是文件）时应判失败并给出路径与原因 */
    @Test
    void unwritableDirFails(@TempDir Path dir) throws Exception {
        Path file = Files.createFile(dir.resolve("not-a-dir"));
        HealthCheckService.Probe probe = service.checkStorageWritable(file.resolve("child").toString());
        assertFalse(probe.ok());
        assertTrue(probe.message().contains("不可写"));
    }

    @Test
    void blankDirFails() {
        assertFalse(service.checkStorageWritable("  ").ok());
    }

    @Test
    void diskCheckPassesWithLowThreshold(@TempDir Path dir) {
        properties.setMinFreeDiskMb(1);
        assertTrue(service.checkDisk(dir.toString()).ok());
    }

    /** 阈值设得极高以模拟"磁盘将满"：必须判失败而不是忽略 */
    @Test
    void diskTooFullFails(@TempDir Path dir) {
        properties.setMinFreeDiskMb(Integer.MAX_VALUE);
        HealthCheckService.Probe probe = service.checkDisk(dir.toString());
        assertFalse(probe.ok());
        assertTrue(probe.message().contains("磁盘余量不足"));
    }

    private void verifyTablesChecked() {
        // 断言回查确实按清单全量发起（漏传会导致"表都在"的假阳性）
        org.mockito.Mockito.verify(mapper).findExistingTables(anyList());
    }
}
