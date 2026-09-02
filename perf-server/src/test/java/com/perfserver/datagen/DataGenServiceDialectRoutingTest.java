package com.perfserver.datagen;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.source.EndpointScanner;
import com.perfserver.source.SourceFileProvider;
import com.perfserver.mapper.AppInstanceMapper;
import com.perfserver.mapper.SourceRepoMapper;
import com.perftestor.datagen.DefaultDataGenEngine;
import com.perftestor.datagen.dialect.mysql.MySQLDialect;
import com.perftestor.datagen.dialect.postgres.PostgreSQLDialect;
import com.perftestor.datagen.executor.JdbcBatchExecutor;
import com.perftestor.datagen.spi.DatabaseDialect;
import com.perfserver.mapper.DataGenTaskMapper;
import com.perfserver.mapper.SemanticHintMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 灌数据服务的多库种路由单测：URL 校验与 URL 增强必须按已注册方言判别，
 * 且各库种参数互不串用（MySQL rewriteBatchedStatements / PG reWriteBatchedInserts）。
 *
 * <p>校验入口取 {@code listTables}（内部先 validateDbUrl 再 connect）：
 * 校验失败抛「JDBC URL 仅支持…」，校验通过后才会进入连接阶段抛「连接目标库失败」，
 * 据此可区分两阶段行为。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class DataGenServiceDialectRoutingTest {

    @Mock
    private DataGenTaskMapper taskMapper;
    @Mock
    private SemanticHintMapper hintMapper;
    @Mock
    private DataGenSpecAdapter specAdapter;
    @Mock
    private SourceFileProvider sourceFileProvider;
    @Mock
    private EndpointScanner endpointScanner;
    @Mock
    private AppInstanceMapper appInstanceMapper;
    @Mock
    private SourceRepoMapper sourceRepoMapper;
    @Mock
    private com.perfserver.mapper.DataGenPlanMapper planMapper;
    @Mock
    private com.perfserver.mapper.DbConnMapper dbConnMapper;
    @Mock
    private com.perfserver.security.CryptoService cryptoService;
    @Mock
    private com.perfserver.datagen.plan.EndpointDataPlanService planService;

    private DataGenService service;

    @BeforeEach
    void setUp() {
        List<DatabaseDialect> dialects = List.of(new MySQLDialect(), new PostgreSQLDialect());
        DefaultDataGenEngine engine = new DefaultDataGenEngine(dialects, new JdbcBatchExecutor(), List.of());
        service = new DataGenService(taskMapper, hintMapper, new ObjectMapper(), specAdapter, engine,
                sourceFileProvider, endpointScanner, appInstanceMapper, sourceRepoMapper,
                planMapper, dbConnMapper, cryptoService, planService);
    }

    @Test
    void postgresqlUrlPassesValidation() {
        // 校验通过（进入连接阶段才失败），证明 PG URL 已被接受
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> service.listTables("jdbc:postgresql://127.0.0.1:1/nodb", "u", "p"));
        assertTrue(e.getMessage().contains("连接目标库失败"), "PG URL 应通过校验，实际: " + e.getMessage());
    }

    @Test
    void mysqlUrlStillAccepted() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> service.listTables("jdbc:mysql://127.0.0.1:1/nodb", "u", "p"));
        assertTrue(e.getMessage().contains("连接目标库失败"), "MySQL URL 应通过校验，实际: " + e.getMessage());
    }

    @Test
    void unregisteredUrlRejected() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> service.listTables("jdbc:oracle:thin:@h:1521/x", "u", "p"));
        assertTrue(e.getMessage().contains("JDBC URL 仅支持"), "未注册库种应被校验拦截，实际: " + e.getMessage());
    }

    @Test
    void nullUrlRejected() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> service.listTables(null, "u", "p"));
        assertTrue(e.getMessage().contains("JDBC URL 仅支持"));
    }
}
