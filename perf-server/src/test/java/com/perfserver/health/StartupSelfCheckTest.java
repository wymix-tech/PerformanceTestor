package com.perfserver.health;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.io.TempDir;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.when;

/**
 * 启动自检：默认只告警不阻断，严格模式下才终止启动。
 *
 * <p>"不阻断"是刻意选择：容器编排下平台常先于 MySQL 启动，
 * fail-fast 会让"库还没起来"变成"平台起不来"。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class StartupSelfCheckTest {

    @Mock
    private HealthMapper mapper;

    private HealthProperties properties;
    private HealthCheckService checkService;

    @BeforeEach
    void setUp() {
        properties = new HealthProperties();
        properties.setMinFreeDiskMb(1);
        properties.setRequiredTables(new String[]{"source_repo"});
        checkService = new HealthCheckService(mapper, properties);
    }

    private StartupSelfCheck selfCheck(String dir) {
        properties.setStorageDir(dir);
        return new StartupSelfCheck(checkService, properties);
    }

    @Test
    void probeAllCoversDbTablesPartitionStorageDisk(@TempDir Path dir) {
        when(mapper.ping()).thenReturn(1);
        when(mapper.databaseVersion()).thenReturn("8.0.36 / MySQL");
        when(mapper.findExistingTables(anyList())).thenReturn(List.of("source_repo"));
        when(mapper.findPartitionedTables(anyList())).thenReturn(List.of("jvm_metric", "stress_sample"));

        List<HealthCheckService.Probe> probes = selfCheck(dir.toString()).probeAll();

        assertEquals(5, probes.size());
        assertTrue(probes.stream().allMatch(HealthCheckService.Probe::ok));
    }

    /** 关闭开关时完全不探活（不查库、不落日志） */
    @Test
    void disabledSkipsEverything(@TempDir Path dir) {
        properties.setStartupCheckEnabled(false);
        selfCheck(dir.toString()).run(null);

        org.mockito.Mockito.verifyNoInteractions(mapper);
    }

    /** 默认非严格：库不通也只是告警，不能抛异常阻断启动 */
    @Test
    void failuresDoNotBlockByDefault(@TempDir Path dir) {
        when(mapper.ping()).thenThrow(new RuntimeException("db not ready"));

        selfCheck(dir.toString()).run(null);
        // 未抛异常即通过
    }

    /** 严格模式：失败必须终止启动，让编排系统把实例标记为未就绪 */
    @Test
    void failFastThrows(@TempDir Path dir) {
        properties.setFailFast(true);
        when(mapper.ping()).thenThrow(new RuntimeException("db not ready"));

        assertThrows(IllegalStateException.class, () -> selfCheck(dir.toString()).run(null));
    }

    @Test
    void failFastPassesWhenAllOk(@TempDir Path dir) {
        properties.setFailFast(true);
        when(mapper.ping()).thenReturn(1);
        when(mapper.databaseVersion()).thenReturn("8.0.36 / MySQL");
        when(mapper.findExistingTables(anyList())).thenReturn(List.of("source_repo"));
        when(mapper.findPartitionedTables(anyList())).thenReturn(List.of("jvm_metric", "stress_sample"));

        selfCheck(dir.toString()).run(null);
        assertFalse(properties.isFailFast() && properties.isStartupCheckEnabled() == false);
    }
}
