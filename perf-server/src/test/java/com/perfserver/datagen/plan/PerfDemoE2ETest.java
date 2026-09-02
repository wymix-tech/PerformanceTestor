package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.DataGenPlan;
import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.DataGenPlanMapper;
import com.perfserver.mapper.SourceRepoMapper;
import com.perfserver.mapper.StressSampleMapper;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.source.SourceFileProvider;
import com.perftestor.datagen.model.SourceFile;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

/**
 * 端到端验收：读取 perf-demo <b>真实源码树</b>，跑通「接口 → 调用链 → SQL → 表 → 行数 → 规则」全链路。
 *
 * <p>与 {@link EndpointDataPlanServiceTest} 的区别：后者用合成源码验证各分支，
 * 本测试用仓库里真实的 perf-demo 代码（构造器注入 + MyBatis 注解 SQL + N+1 调用），
 * 验证在真实写法下链路不塌。</p>
 *
 * <p>perf-demo 真实形态：</p>
 * <ul>
 *   <li>{@code OrderController}（构造器注入 OrderService → OrderMapper 注解 SQL）→ perf_order</li>
 *   <li>{@code UserController}（构造器注入 UserService → UserMapper，循环内 N+1）→ perf_user + perf_order</li>
 *   <li>{@code DemoScenarioController}（刻意无数据库访问）→ 应产出空表清单 + 提示，而不是报错</li>
 * </ul>
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class PerfDemoE2ETest {

    /** perf-demo 源码根目录（相对本模块） */
    private static final Path DEMO_SRC = Paths.get("../perf-demo/src/main/java");

    @Mock
    private DataGenPlanMapper planMapper;
    @Mock
    private SourceRepoMapper repoMapper;
    @Mock
    private SourceFileProvider sourceFileProvider;
    @Mock
    private StressTaskMapper taskMapper;
    @Mock
    private StressSampleMapper sampleMapper;

    private static List<SourceFile> demoFiles;
    private EndpointDataPlanService service;

    @BeforeAll
    static void loadDemoSources() throws IOException {
        demoFiles = readDemoSources();
    }

    /** 读取 perf-demo 全部 .java/.xml，路径取相对路径（与 DB 索引形态一致） */
    private static List<SourceFile> readDemoSources() throws IOException {
        List<SourceFile> out = new ArrayList<>();
        if (!Files.isDirectory(DEMO_SRC)) {
            return out;
        }
        try (Stream<Path> walk = Files.walk(DEMO_SRC)) {
            for (Path p : walk.filter(Files::isRegularFile).sorted().toList()) {
                String name = p.getFileName().toString();
                if (!name.endsWith(".java") && !name.endsWith(".xml")) {
                    continue;
                }
                String relative = "src/main/java/" + DEMO_SRC.relativize(p).toString().replace('\\', '/');
                out.add(new SourceFile(relative, Files.readString(p, StandardCharsets.UTF_8)));
            }
        }
        return out;
    }

    private EndpointDataPlanService newService() {
        ObjectMapper mapper = new ObjectMapper();
        return new EndpointDataPlanService(planMapper, repoMapper, sourceFileProvider, mapper,
                new RtBaselineResolver(taskMapper, sampleMapper, mapper),
                new PlanRuleComposer(), new LlmPlanEnhancer(null, mapper));
    }

    private void setUpMocks() {
        service = newService();
        when(sourceFileProvider.loadFiles(anyLong())).thenReturn(demoFiles);
        SourceRepo repo = new SourceRepo();
        repo.setAppName("perf-demo");
        when(repoMapper.findById(anyLong())).thenReturn(repo);
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt()))
                .thenReturn(List.of());
    }

    private static EndpointDataPlanService.EndpointInput input(String method, String path) {
        EndpointDataPlanService.EndpointInput in = new EndpointDataPlanService.EndpointInput();
        in.setHttpMethod(method);
        in.setPath(path);
        return in;
    }

    private static EndpointDataPlanService.AnalyzeRequest req(String method, String path) {
        EndpointDataPlanService.AnalyzeRequest r = new EndpointDataPlanService.AnalyzeRequest();
        r.setRepoId(1L);
        r.setDbUrl("jdbc:mysql://127.0.0.1:3306/perf_demo");
        r.getEndpoints().add(input(method, path));
        RowCountEstimator.StressParams stress = new RowCountEstimator.StressParams();
        stress.setConcurrency(50);
        stress.setDurationSec(120);
        r.setStress(stress);
        return r;
    }

    private Map<String, PlanIntegrityChecker.PlanTable> analyze(EndpointDataPlanService.AnalyzeRequest r) {
        setUpMocks();
        List<String> warnings = new ArrayList<>();
        Map<String, PlanIntegrityChecker.PlanTable> m = new java.util.LinkedHashMap<>();
        for (PlanIntegrityChecker.PlanTable t : service.doAnalyze(r, warnings)) {
            m.put(t.getTable(), t);
        }
        return m;
    }

    private List<String> analyzeWarnings(EndpointDataPlanService.AnalyzeRequest r) {
        setUpMocks();
        List<String> warnings = new ArrayList<>();
        service.doAnalyze(r, warnings);
        return warnings;
    }

    @Test
    void demoSourcesAreLoaded() {
        assertFalse(demoFiles.isEmpty(), "perf-demo 源码未读到，验收无效");
        assertTrue(demoFiles.stream().anyMatch(f -> f.getPath().contains("OrderController")),
                "应包含 OrderController");
        assertTrue(demoFiles.stream().anyMatch(f -> f.getPath().contains("UserController")),
                "应包含 UserController");
    }

    // ===== 验收 1：GET 列表接口 → 表与读写类型 =====

    @Test
    void orderListResolvesPerfOrderAsRead() {
        var tables = analyze(req("GET", "/api/order/list"));
        assertTrue(tables.containsKey("perf_order"), "GET /api/order/list 应涉及 perf_order，实际: " + tables.keySet());
        assertEquals("READ", tables.get("perf_order").getAccess());
    }

    @Test
    void orderListEvidencePointsToMapperCallSite() {
        var tables = analyze(req("GET", "/api/order/list"));
        List<String> evidence = tables.get("perf_order").getEvidence();
        assertFalse(evidence.isEmpty(), "必须带 文件:行号 证据");
        assertTrue(evidence.stream().anyMatch(e -> e.matches(".+:\\d+")), "证据格式应为 文件:行号，实际: " + evidence);
        assertTrue(evidence.stream().anyMatch(e -> e.contains("OrderService.java")),
                "证据应落在调用点（OrderService），实际: " + evidence);
    }

    @Test
    void statsEndpointResolvesSameTable() {
        var tables = analyze(req("GET", "/api/order/stats"));
        assertTrue(tables.containsKey("perf_order"), "聚合统计应涉及 perf_order，实际: " + tables.keySet());
    }

    @Test
    void recentEndpointResolvesSameTable() {
        var tables = analyze(req("GET", "/api/order/recent"));
        assertTrue(tables.containsKey("perf_order"), "实际: " + tables.keySet());
    }

    // ===== 验收 2：JOIN / 多表接口（perf-demo 的 N+1 形态） =====

    @Test
    void userListCoversBothTablesOfTheN1Loop() {
        var tables = analyze(req("GET", "/api/user/list"));
        // UserService.listUsersWithOrderCount 里 selectAll(perf_user) + 循环 countOrders(perf_order)
        assertTrue(tables.containsKey("perf_user"), "N+1 主表应覆盖，实际: " + tables.keySet());
        assertTrue(tables.containsKey("perf_order"), "N+1 循环内的订单表也应覆盖，实际: " + tables.keySet());
    }

    @Test
    void bothTablesInN1AreMarkedRead() {
        var tables = analyze(req("GET", "/api/user/list"));
        assertEquals("READ", tables.get("perf_user").getAccess());
        assertEquals("READ", tables.get("perf_order").getAccess());
    }

    // ===== 验收 3：无数据库访问的接口 → 空清单 + 提示，不报错 =====

    @Test
    void scenarioEndpointWithoutDbAccessYieldsEmptyTablesNotError() {
        var tables = analyze(req("POST", "/api/scenario/order"));
        assertTrue(tables.isEmpty(),
                "DemoScenarioController 刻意无数据库访问，应产出空表清单并提示补表，实际: " + tables.keySet());
    }

    @Test
    void scenarioEndpointRecordsUncoveredWarning() {
        List<String> warnings = analyzeWarnings(req("POST", "/api/scenario/order"));
        assertTrue(warnings.stream().anyMatch(w -> w.contains("未从源码分析出")),
                "无 DB 访问的接口必须给出可解释的提示，不能静默返回空清单，实际: " + warnings);
    }

    // ===== 验收 4：行数按压测参数推导 =====

    @Test
    void rowCountGrowsWithConcurrency() {
        var light = analyze(req("GET", "/api/order/list"));
        EndpointDataPlanService.AnalyzeRequest heavy = req("GET", "/api/order/list");
        heavy.getStress().setConcurrency(500);
        var heavyTables = analyze(heavy);
        assertTrue(heavyTables.get("perf_order").getRowCount() >= light.get("perf_order").getRowCount(),
                "并发更大时行数不应更少");
    }

    @Test
    void rowCountRespectsMinimum() {
        var tables = analyze(req("GET", "/api/order/list"));
        assertTrue(tables.get("perf_order").getRowCount() >= RowCountEstimator.MIN_MAIN_ROWS,
                "行数应有下限，实际: " + tables.get("perf_order").getRowCount());
    }

    @Test
    void rtBaselineSourceIsRecordedInWarnings() {
        List<String> warnings = analyzeWarnings(req("GET", "/api/order/list"));
        assertTrue(warnings.stream().anyMatch(w -> w.contains("响应时间基线取值来源")),
                "RT 来源必须可追溯，实际: " + warnings);
    }

    // ===== 验收 5：方案落库与回看 =====

    @Test
    void analyzePersistsPlanWithWarningsForReview() {
        setUpMocks();
        when(planMapper.insert(org.mockito.ArgumentMatchers.any(DataGenPlan.class)))
                .thenAnswer(inv -> {
                    DataGenPlan p = inv.getArgument(0);
                    p.setId(101L);
                    return 1;
                });
        when(planMapper.findById(anyLong())).thenAnswer(inv -> {
            DataGenPlan p = new DataGenPlan();
            p.setId(inv.getArgument(0));
            return p;
        });

        DataGenPlan plan = service.analyze(req("GET", "/api/order/list"));
        assertNotNull(plan.getId(), "方案必须落库并拿到 ID，才能回看与重复执行");
        assertEquals(DataGenPlan.ST_PENDING, plan.getStatus());
    }

    @Test
    void planTablesJsonRoundTripsThroughService() throws Exception {
        setUpMocks();
        List<String> warnings = new ArrayList<>();
        List<PlanIntegrityChecker.PlanTable> tables = service.doAnalyze(req("GET", "/api/order/list"), warnings);
        String json = new ObjectMapper().writeValueAsString(tables);
        List<PlanIntegrityChecker.PlanTable> parsed = service.parseTables(json);
        assertEquals(tables.size(), parsed.size(), "方案落库 JSON 必须能原样读回（回看/重复执行依赖）");
        for (int i = 0; i < tables.size(); i++) {
            assertEquals(tables.get(i).getTable(), parsed.get(i).getTable());
            assertEquals(tables.get(i).getRowCount(), parsed.get(i).getRowCount());
        }
    }

    // ===== 验收 6：接口未定位的降级 =====

    @Test
    void unknownPathIsReportedNotGuessed() {
        List<String> warnings = analyzeWarnings(req("GET", "/api/order/notExist"));
        assertTrue(warnings.stream().anyMatch(w -> w.contains("接口未定位")),
                "未定位接口必须告警而非猜测，实际: " + warnings);
    }
}
