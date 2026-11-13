package com.perfserver.health;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.io.TempDir;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;
import org.springframework.boot.actuate.health.Status;

import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.when;

/**
 * /actuator/health 的 platform 细节项：db 与磁盘任一不可用即 DOWN。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class PlatformHealthIndicatorTest {

    @Mock
    private HealthMapper mapper;

    private HealthProperties properties;
    private HealthCheckService checkService;

    @BeforeEach
    void setUp() {
        properties = new HealthProperties();
        properties.setMinFreeDiskMb(1);
        checkService = new HealthCheckService(mapper, properties);
    }

    private PlatformHealthIndicator indicator(String dir) {
        properties.setStorageDir(dir);
        return new PlatformHealthIndicator(checkService, properties);
    }

    @Test
    void upWhenDbReachableAndDiskOk(@TempDir Path dir) {
        when(mapper.ping()).thenReturn(1);
        when(mapper.databaseVersion()).thenReturn("8.0.36 / MySQL");

        var health = indicator(dir.toString()).health();

        assertEquals(Status.UP, health.getStatus());
        assertEquals(true, health.getDetails().containsKey("database"));
        assertEquals(true, health.getDetails().containsKey("storage"));
    }

    /** 库不可达 → DOWN：否则健康检查会在服务实际不可用时仍报 UP */
    @Test
    void downWhenDbUnreachable(@TempDir Path dir) {
        when(mapper.ping()).thenThrow(new RuntimeException("down"));

        assertEquals(Status.DOWN, indicator(dir.toString()).health().getStatus());
    }

    /** 磁盘将满 → DOWN */
    @Test
    void downWhenDiskFull(@TempDir Path dir) {
        when(mapper.ping()).thenReturn(1);
        when(mapper.databaseVersion()).thenReturn("8.0.36 / MySQL");
        properties.setMinFreeDiskMb(Integer.MAX_VALUE);

        assertEquals(Status.DOWN, indicator(dir.toString()).health().getStatus());
    }
}
