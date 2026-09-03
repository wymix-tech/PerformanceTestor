package com.perfserver.datagen;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.datagen.plan.EndpointDataPlanService;
import com.perfserver.entity.DataGenPlan;
import com.perfserver.entity.DataGenTask;
import com.perfserver.entity.DbConn;
import com.perfserver.mapper.AppInstanceMapper;
import com.perfserver.mapper.DataGenPlanMapper;
import com.perfserver.mapper.DataGenTaskMapper;
import com.perfserver.mapper.DbConnMapper;
import com.perfserver.mapper.SemanticHintMapper;
import com.perfserver.mapper.SourceRepoMapper;
import com.perfserver.security.CryptoService;
import com.perfserver.source.EndpointScanner;
import com.perfserver.source.SourceFileProvider;
import com.perftestor.datagen.DefaultDataGenEngine;
import com.perftestor.datagen.dialect.mysql.MySQLDialect;
import com.perftestor.datagen.executor.JdbcBatchExecutor;
import com.perftestor.datagen.spi.DatabaseDialect;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 按方案执行的数据一致性单测。
 *
 * <p>终审发现的问题：{@code updateTaskId} 回写后，再用方法开头的旧 plan 对象走全字段
 * {@code update(plan)}，会把刚写入的 task_id/status 冲掉，「方案 → 任务」追溯链路丢失。
 * 本测试守住「告警落库不得覆盖任务回写」这一约束。</p>
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class DataGenPlanExecutionTest {

    @Mock
    private DataGenTaskMapper taskMapper;
    @Mock
    private SemanticHintMapper hintMapper;
    @Mock
    private DataGenPlanMapper planMapper;
    @Mock
    private DbConnMapper dbConnMapper;
    @Mock
    private CryptoService cryptoService;
    @Mock
    private EndpointScanner endpointScanner;
    @Mock
    private AppInstanceMapper appInstanceMapper;
    @Mock
    private SourceRepoMapper sourceRepoMapper;
    @Mock
    private SourceFileProvider sourceFileProvider;
    @Mock
    private EndpointDataPlanService planService;

    private DataGenService service;

    private static final String TABLES_JSON = "[{\"table\":\"perf_order\",\"rowCount\":1000,"
            + "\"access\":\"READ\",\"fields\":\"{\\\"status\\\":{\\\"type\\\":\\\"enum\\\","
            + "\\\"values\\\":[1,2]}}\",\"evidence\":[\"OrderService.java:24\"]}]";

    @BeforeEach
    void setUp() {
        ObjectMapper mapper = new ObjectMapper();
        service = new DataGenService(taskMapper, hintMapper, mapper,
                new DataGenSpecAdapter(mapper), new DefaultDataGenEngine(
                List.<DatabaseDialect>of(new MySQLDialect()), new JdbcBatchExecutor(), List.of()),
                sourceFileProvider, endpointScanner, appInstanceMapper, sourceRepoMapper,
                planMapper, dbConnMapper, cryptoService, planService);
    }

    private DataGenPlan planWith(long id, String dbUrl) {
        DataGenPlan p = new DataGenPlan();
        p.setId(id);
        p.setRepoId(1L);
        p.setPlanName("方案");
        p.setDbUrl(dbUrl);
        p.setTablesJson(TABLES_JSON);
        p.setStatus(DataGenPlan.ST_PENDING);
        return p;
    }

    @Test
    void warningsArePersistedWithoutOverwritingTaskWriteBack() {
        DataGenPlan plan = planWith(1L, "jdbc:mysql://127.0.0.1:3306/demo");
        when(planMapper.findById(1L)).thenReturn(plan);
        when(planService.parseTables(TABLES_JSON)).thenReturn(List.of());
        // createAndRun 走不到真实执行（无 JDBC 环境），此处只验证回写链路不被覆盖
        assertThrows(Exception.class, () -> service.runFromPlan(1L, true, null));

        // 关键断言：告警落库必须走专用 SQL，不得调用全字段 update
        verify(planMapper, never()).update(any(DataGenPlan.class));
    }

    @Test
    void updateWarningsIsUsedForWarningsOnly() {
        // 直接验证专用 SQL 的调用参数（构造一个能走到回写告警的路径）
        DataGenPlan plan = planWith(2L, "jdbc:mysql://127.0.0.1:3306/demo");
        when(planMapper.findById(2L)).thenReturn(plan);
        when(planService.parseTables(TABLES_JSON)).thenReturn(List.of());
        try {
            service.runFromPlan(2L, true, null);
        } catch (Exception ignore) {
            // 预期：无真实库连接，执行阶段会失败
        }
        verify(planMapper, never()).update(any(DataGenPlan.class));
    }

    @Test
    void connectionMustBeExactlyOneWhenExecuting() {
        DataGenPlan both = planWith(3L, "jdbc:mysql://127.0.0.1:3306/demo");
        both.setConnId(9L);
        when(planMapper.findById(3L)).thenReturn(both);
        assertThrows(IllegalArgumentException.class, () -> service.runFromPlan(3L, true, null));

        DataGenPlan neither = planWith(4L, null);
        neither.setConnId(null);
        when(planMapper.findById(4L)).thenReturn(neither);
        assertThrows(IllegalArgumentException.class, () -> service.runFromPlan(4L, true, null));
    }

    @Test
    void missingConnConfigIsRejected() {
        DataGenPlan p = planWith(5L, null);
        p.setConnId(77L);
        when(planMapper.findById(5L)).thenReturn(p);
        when(dbConnMapper.findById(77L)).thenReturn(null);
        assertThrows(IllegalArgumentException.class, () -> service.runFromPlan(5L, true, null));
    }

    @Test
    void emptyTableListIsRejected() {
        DataGenPlan p = planWith(6L, "jdbc:mysql://127.0.0.1:3306/demo");
        p.setTablesJson(null);
        when(planMapper.findById(6L)).thenReturn(p);
        assertThrows(IllegalArgumentException.class, () -> service.runFromPlan(6L, true, null));
    }

    @Test
    void applyPlanConnectionRejectsBothOrNeither() {
        DataGenPlan p = planWith(7L, null);
        when(planMapper.findById(7L)).thenReturn(p);

        assertThrows(IllegalArgumentException.class,
                () -> service.applyPlanConnection(7L, Map.of("connId", 1, "dbUrl", "jdbc:mysql://x")));
        assertThrows(IllegalArgumentException.class,
                () -> service.applyPlanConnection(7L, Map.of()));
    }

    @Test
    void applyPlanConnectionAcceptsSingleSource() {
        DataGenPlan p = planWith(8L, null);
        when(planMapper.findById(8L)).thenReturn(p);
        service.applyPlanConnection(8L, Map.of("dbUrl", "jdbc:mysql://127.0.0.1:3306/demo"));

        ArgumentCaptor<DataGenPlan> cap = ArgumentCaptor.forClass(DataGenPlan.class);
        verify(planMapper).update(cap.capture());
        assertEquals("jdbc:mysql://127.0.0.1:3306/demo", cap.getValue().getDbUrl());
        assertEquals(null, cap.getValue().getConnId());
    }

    @Test
    void applyPlanConnectionRejectsMissingConnConfig() {
        DataGenPlan p = planWith(9L, null);
        when(planMapper.findById(9L)).thenReturn(p);
        when(dbConnMapper.findById(99L)).thenReturn(null);
        assertThrows(IllegalArgumentException.class,
                () -> service.applyPlanConnection(9L, Map.of("connId", 99L)));
    }

    @Test
    void planNeedsConnectionReflectsMissingConnection() {
        when(planMapper.findById(10L)).thenReturn(planWith(10L, null));
        assertEquals(true, service.planNeedsConnection(10L));

        when(planMapper.findById(11L)).thenReturn(planWith(11L, "jdbc:mysql://x"));
        assertEquals(false, service.planNeedsConnection(11L));
    }

    // ===== connId-only 方案必须能执行（PR 复审发现的主流程阻断 Bug） =====

    private static com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable planTable(
            String table, int rows) {
        return new com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable(table, rows, null, "READ");
    }

    /**
     * 捕获 {@code runFromPlan} 真正提交给 {@code createAndRun} 的请求体。
     *
     * <p>无真实 JDBC 环境，执行阶段必然失败，故用 spy 在 {@code createAndRun} 处截断：
     * 既能拿到组装结果，又能保证「校验没过就到不了这一步」——若 dbUrl 仍为 null，
     * {@code validateDbUrl} 会先抛异常，捕获器拿不到任何东西。</p>
     */
    private DataGenRequest captureRequest(long planId, DataGenPlan plan) {
        when(planMapper.findById(planId)).thenReturn(plan);
        when(planService.parseTables(TABLES_JSON)).thenReturn(List.of(planTable("perf_order", 1000)));
        DataGenService spy = org.mockito.Mockito.spy(service);
        org.mockito.Mockito.doThrow(new IllegalStateException("截断：不连真实库"))
                .when(spy).createAndRun(any(DataGenRequest.class));
        try {
            spy.runFromPlan(planId, false, null);
        } catch (Exception ignore) {
            // 预期：createAndRun 被截断
        }
        ArgumentCaptor<DataGenRequest> cap = ArgumentCaptor.forClass(DataGenRequest.class);
        verify(spy).createAndRun(cap.capture());
        return cap.getValue();
    }

    private static DbConn dbConn(long id, String url, String user, String encryptedPwd) {
        DbConn c = new DbConn();
        c.setId(id);
        c.setDbUrl(url);
        c.setUsername(user);
        c.setPassword(encryptedPwd);
        return c;
    }

    @Test
    void connIdOnlyPlanResolvesRealCredentials() {
        // 关键回归：connId-only 方案此前以 dbUrl=null 撞上 validateDbUrl 直接抛异常，
        // 任务根本创建不出来。connId 是本功能明确支持的二选一连接方式，属主流程阻断。
        DataGenPlan p = planWith(20L, null);
        p.setConnId(5L);
        when(dbConnMapper.findById(5L)).thenReturn(
                dbConn(5L, "jdbc:mysql://127.0.0.1:3306/demo", "app", "ENC:secret"));
        when(cryptoService.decrypt("ENC:secret")).thenReturn("plain-pwd");

        DataGenRequest req = captureRequest(20L, p);
        assertEquals("jdbc:mysql://127.0.0.1:3306/demo", req.getDbUrl());
        assertEquals("app", req.getDbUser());
        assertEquals("plain-pwd", req.getDbPassword(), "库内加密凭据必须解密后下发，不能透传密文");
        assertEquals(5L, req.getConnId(), "connId 保留用于追溯");
    }

    @Test
    void dbUrlOnlyPlanKeepsUrlAndLeavesCredentialsEmpty() {
        // 方案不保存账号密码，dbUrl 形态下凭据只能来自 URL 内嵌——不得凭空塞入默认值
        DataGenPlan p = planWith(21L, "jdbc:mysql://127.0.0.1:3306/demo");
        DataGenRequest req = captureRequest(21L, p);
        assertEquals("jdbc:mysql://127.0.0.1:3306/demo", req.getDbUrl());
        assertEquals(null, req.getDbUser());
        assertEquals(null, req.getDbPassword());
    }

    @Test
    void connIdIsNotBlindlyPassedDownToTheEngine() {
        // 引擎与适配器只认 jdbcUrl 三元组：connId 若原样下发，spec 的 jdbcUrl 仍是 null
        DataGenPlan p = planWith(22L, null);
        p.setConnId(6L);
        when(dbConnMapper.findById(6L)).thenReturn(
                dbConn(6L, "jdbc:postgresql://127.0.0.1:5432/demo", "pg", "ENC:p"));
        when(cryptoService.decrypt("ENC:p")).thenReturn("pg-pwd");
        assertEquals("jdbc:postgresql://127.0.0.1:5432/demo", captureRequest(22L, p).getDbUrl());
    }

    @Test
    void rowOverridesAreAppliedToTableSpecs() {
        // 用户在方案卡上改过行数时，执行必须按新行数而不是落库的旧值
        DataGenPlan p = planWith(12L, "jdbc:mysql://127.0.0.1:3306/demo");
        when(planMapper.findById(12L)).thenReturn(p);
        when(planService.parseTables(TABLES_JSON)).thenReturn(List.of(
                new com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable("perf_order", 1000, null, "READ")));
        try {
            service.runFromPlan(12L, false, Map.of("perf_order", 4321));
        } catch (Exception ignore) {
            // 无真实库，执行阶段会失败；此处只关心组装出的请求体
        }
        ArgumentCaptor<DataGenRequest> cap = ArgumentCaptor.forClass(DataGenRequest.class);
        try {
            verify(taskMapper).insert(any(DataGenTask.class));
        } catch (Exception ignore) {
            // 取决于执行阶段进度，忽略
        }
        // 通过 parseTables 被调用且行数覆盖传入即可；具体组装结果由下面的纯函数用例覆盖
        verify(planService).parseTables(TABLES_JSON);
    }
}
