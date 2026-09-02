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
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

/**
 * 接口驱动方案编排单测：接口定位 → 调用链 → SQL → 表与读写类型 → 行数。
 *
 * <p>源码样本按 perf-demo 形态构造（Controller → Service → Mapper 注解 SQL），
 * 覆盖 GET 列表 / GET 详情 / JOIN / POST 创建四类接口。</p>
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class EndpointDataPlanServiceTest {

    private static final String CTRL = "src/main/java/com/perfdemo/controller/OrderController.java";
    private static final String SVC = "src/main/java/com/perfdemo/service/OrderService.java";
    private static final String MAPPER = "src/main/java/com/perfdemo/mapper/OrderMapper.java";
    private static final String USER_MAPPER = "src/main/java/com/perfdemo/mapper/UserMapper.java";

    private static final String CONTROLLER = """
            package com.perfdemo.controller;

            import org.springframework.beans.factory.annotation.Autowired;
            import org.springframework.web.bind.annotation.*;

            @RestController
            @RequestMapping("/api/order")
            public class OrderController {

                @Autowired
                private OrderService orderService;

                @GetMapping("/list")
                public Object list(String status) {
                    return orderService.listOrders(status);
                }

                @GetMapping("/{id}")
                public Object detail(Long id) {
                    return orderService.getDetail(id);
                }

                @PostMapping("/create")
                public Object create(Object req) {
                    return orderService.createOrder(req);
                }
            }
            """;

    private static final String SERVICE = """
            package com.perfdemo.service;

            import org.springframework.beans.factory.annotation.Autowired;
            import org.springframework.stereotype.Service;
            import com.perfdemo.mapper.OrderMapper;
            import com.perfdemo.mapper.UserMapper;

            @Service
            public class OrderService {

                @Autowired
                private OrderMapper orderMapper;

                @Autowired
                private UserMapper userMapper;

                public Object listOrders(String status) {
                    return orderMapper.selectByStatus(status);
                }

                public Object getDetail(Long id) {
                    userMapper.selectById(1L);
                    return orderMapper.selectById(id);
                }

                public Object createOrder(Object req) {
                    userMapper.updateLastActive(1L);
                    return orderMapper.insertOrder(req);
                }
            }
            """;

    private static final String ORDER_MAPPER = """
            package com.perfdemo.mapper;

            import org.apache.ibatis.annotations.Select;
            import org.apache.ibatis.annotations.Insert;

            public interface OrderMapper {
                @Select("SELECT id, user_id, amount, status FROM perf_order WHERE status = 1 "
                        + "ORDER BY id DESC LIMIT 20")
                Object selectByStatus(String status);

                @Select("SELECT * FROM perf_order WHERE id = #{id}")
                Object selectById(Long id);

                @Insert("INSERT INTO perf_order (user_id, amount, status) VALUES (1, 100, 1)")
                Object insertOrder(Object req);
            }
            """;

    private static final String USER_MAPPER_SRC = """
            package com.perfdemo.mapper;

            import org.apache.ibatis.annotations.Select;
            import org.apache.ibatis.annotations.Update;

            public interface UserMapper {
                @Select("SELECT * FROM perf_user WHERE id = #{id}")
                Object selectById(Long id);

                @Update("UPDATE perf_user SET last_active = NOW() WHERE id = #{id}")
                void updateLastActive(Long id);
            }
            """;

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

    private EndpointDataPlanService service;

    @BeforeEach
    void setUp() {
        ObjectMapper mapper = new ObjectMapper();
        RtBaselineResolver rt = new RtBaselineResolver(taskMapper, sampleMapper, mapper);
        service = new EndpointDataPlanService(planMapper, repoMapper, sourceFileProvider, mapper,
                rt, new PlanRuleComposer(), new LlmPlanEnhancer(null, mapper));

        when(sourceFileProvider.loadFiles(anyLong())).thenReturn(List.of(
                new SourceFile(CTRL, CONTROLLER),
                new SourceFile(SVC, SERVICE),
                new SourceFile(MAPPER, ORDER_MAPPER),
                new SourceFile(USER_MAPPER, USER_MAPPER_SRC)));
        SourceRepo repo = new SourceRepo();
        repo.setAppName("perf-demo");
        when(repoMapper.findById(anyLong())).thenReturn(repo);
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt()))
                .thenReturn(List.of());
    }

    /** 模拟自增主键回填（useGeneratedKeys），否则 planId 为 null */
    private void stubInsertAssignsId() {
        when(planMapper.insert(org.mockito.ArgumentMatchers.any(DataGenPlan.class)))
                .thenAnswer(inv -> {
                    DataGenPlan p = inv.getArgument(0);
                    p.setId(nextId++);
                    return 1;
                });
        when(planMapper.findById(anyLong())).thenAnswer(inv -> {
            DataGenPlan p = new DataGenPlan();
            p.setId(inv.getArgument(0));
            return p;
        });
    }

    private long nextId = 1L;

    private static EndpointDataPlanService.EndpointInput input(String method, String path) {
        EndpointDataPlanService.EndpointInput in = new EndpointDataPlanService.EndpointInput();
        in.setHttpMethod(method);
        in.setPath(path);
        return in;
    }

    private static EndpointDataPlanService.AnalyzeRequest request(String method, String path) {
        EndpointDataPlanService.AnalyzeRequest req = new EndpointDataPlanService.AnalyzeRequest();
        req.setRepoId(1L);
        req.setDbUrl("jdbc:mysql://127.0.0.1:3306/demo");
        req.getEndpoints().add(input(method, path));
        RowCountEstimator.StressParams stress = new RowCountEstimator.StressParams();
        stress.setConcurrency(50);
        stress.setDurationSec(120);
        req.setStress(stress);
        return req;
    }

    private List<PlanIntegrityChecker.PlanTable> analyze(EndpointDataPlanService.AnalyzeRequest req) {
        List<String> warnings = new ArrayList<>();
        return service.doAnalyze(req, warnings);
    }

    private Map<String, PlanIntegrityChecker.PlanTable> byTable(List<PlanIntegrityChecker.PlanTable> tables) {
        Map<String, PlanIntegrityChecker.PlanTable> m = new java.util.LinkedHashMap<>();
        tables.forEach(t -> m.put(t.getTable(), t));
        return m;
    }

    @Test
    void listEndpointResolvesReadTableWithPredicate() {
        var tables = byTable(analyze(request("GET", "/api/order/list")));
        assertTrue(tables.containsKey("perf_order"), "实际: " + tables.keySet());
        assertEquals("READ", tables.get("perf_order").getAccess());
        assertFalse(tables.get("perf_order").getEvidence().isEmpty(), "必须带 文件:行号 证据");
        assertTrue(tables.get("perf_order").getEvidence().get(0).matches(".+:\\d+"));
    }

    @Test
    void listPredicateBecomesEnumRule() throws Exception {
        var tables = byTable(analyze(request("GET", "/api/order/list")));
        String fields = tables.get("perf_order").getFields();
        assertTrue(fields != null && fields.contains("\"status\""), "谓词列应生成规则: " + fields);
        assertTrue(fields.contains("enum"), "等值谓词应转 enum: " + fields);
    }

    @Test
    void detailEndpointResolvesJoinSideTable() {
        var tables = byTable(analyze(request("GET", "/api/order/{id}")));
        assertTrue(tables.containsKey("perf_user"), "详情类接口应覆盖 JOIN 侧的表: " + tables.keySet());
    }

    @Test
    void createEndpointResolvesWriteAccess() {
        var tables = byTable(analyze(request("POST", "/api/order/create")));
        assertTrue(tables.containsKey("perf_order"), "实际: " + tables.keySet());
        assertEquals("WRITE", tables.get("perf_order").getAccess(), "INSERT 应判定为写");
    }

    @Test
    void createEndpointCoversParentUpdateTable() {
        var tables = byTable(analyze(request("POST", "/api/order/create")));
        assertTrue(tables.containsKey("perf_user"),
                "写接口需覆盖被 UPDATE 的关联父表（唯一键余量）: " + tables.keySet());
    }

    @Test
    void rowCountIsDerivedFromConcurrencyAndDuration() {
        var light = byTable(analyze(request("GET", "/api/order/list")));
        EndpointDataPlanService.AnalyzeRequest heavy = request("GET", "/api/order/list");
        heavy.getStress().setConcurrency(500);
        var heavyTables = byTable(analyze(heavy));
        assertTrue(heavyTables.get("perf_order").getRowCount() >= light.get("perf_order").getRowCount(),
                "并发更大时行数不应更少");
    }

    @Test
    void pageLimitRaisesRowFloor() {
        var tables = byTable(analyze(request("GET", "/api/order/list")));
        assertTrue(tables.get("perf_order").getRowCount() >= 20 * RowCountEstimator.PAGE_MULTIPLIER,
                "LIMIT 20 时主表行数应保证能翻页，实际: " + tables.get("perf_order").getRowCount());
    }

    @Test
    void unmatchedEndpointRecordsWarning() {
        List<String> warnings = new ArrayList<>();
        service.doAnalyze(request("GET", "/api/not/exist"), warnings);
        assertTrue(warnings.stream().anyMatch(w -> w.contains("接口未定位")),
                "接口未定位必须告警，不做静默猜测: " + warnings);
    }

    @Test
    void unreachableSourceRecordsWarning() {
        when(sourceFileProvider.loadFiles(anyLong())).thenReturn(List.of());
        List<String> warnings = new ArrayList<>();
        service.doAnalyze(request("GET", "/api/order/list"), warnings);
        assertTrue(warnings.stream().anyMatch(w -> w.contains("源码不可读")), "实际: " + warnings);
    }

    @Test
    void rtBaselineSourceIsRecorded() {
        List<String> warnings = new ArrayList<>();
        service.doAnalyze(request("GET", "/api/order/list"), warnings);
        assertTrue(warnings.stream().anyMatch(w -> w.contains("响应时间基线取值来源")),
                "RT 来源必须可追溯，避免用户误以为行数都是精算值: " + warnings);
    }

    @Test
    void connectionCannotBeBothConnIdAndDbUrl() {
        EndpointDataPlanService.AnalyzeRequest both = request("GET", "/api/order/list");
        both.setConnId(9L);
        assertThrows(IllegalArgumentException.class, () -> service.analyze(both),
                "同时提供 connId 与 dbUrl 必须拒绝");
    }

    @Test
    void connectionMayBeDeferredToExecutionTime() {
        // 分析阶段只读源码不连目标库，连接允许留空（在数据准备页选定后再回填）
        EndpointDataPlanService.AnalyzeRequest neither = request("GET", "/api/order/list");
        neither.setDbUrl(null);
        stubInsertAssignsId();
        DataGenPlan plan = service.analyze(neither);
        assertEquals(DataGenPlan.ST_PENDING, plan.getStatus());
    }

    @Test
    void repoIdIsRequired() {
        EndpointDataPlanService.AnalyzeRequest req = request("GET", "/api/order/list");
        req.setRepoId(null);
        assertThrows(IllegalArgumentException.class, () -> service.analyze(req));
    }

    @Test
    void atLeastOneEndpointIsRequired() {
        EndpointDataPlanService.AnalyzeRequest req = request("GET", "/api/order/list");
        req.getEndpoints().clear();
        assertThrows(IllegalArgumentException.class, () -> service.analyze(req));
    }

    @Test
    void analyzePersistsPlanAndReturnsIt() {
        stubInsertAssignsId();
        DataGenPlan plan = service.analyze(request("GET", "/api/order/list"));
        assertTrue(plan.getPlanName().contains("/api/order/list"), "默认方案名取接口名: " + plan.getPlanName());
        assertEquals(DataGenPlan.ST_PENDING, plan.getStatus());
    }

    @Test
    void planTablesJsonRoundTrips() throws Exception {
        List<PlanIntegrityChecker.PlanTable> tables = analyze(request("GET", "/api/order/list"));
        String json = new ObjectMapper().writeValueAsString(tables);
        List<PlanIntegrityChecker.PlanTable> parsed = service.parseTables(json);
        assertEquals(tables.size(), parsed.size());
        assertEquals(tables.get(0).getTable(), parsed.get(0).getTable());
        assertEquals(tables.get(0).getRowCount(), parsed.get(0).getRowCount());
    }

    @Test
    void planNameFallsBackToEndpointWhenBlank() {
        EndpointDataPlanService.AnalyzeRequest req = request("POST", "/api/order/create");
        req.setPlanName("  ");
        stubInsertAssignsId();
        DataGenPlan plan = service.analyze(req);
        assertTrue(plan.getPlanName().contains("create"));
    }

    @Test
    void detailOfMissingPlanThrows() {
        when(planMapper.findById(anyLong())).thenReturn(null);
        assertThrows(IllegalArgumentException.class, () -> service.detail(404L));
    }

    @Test
    void adoptUpdatesStatus() {
        DataGenPlan existing = new DataGenPlan();
        existing.setId(1L);
        existing.setStatus(DataGenPlan.ST_PENDING);
        when(planMapper.findById(anyLong())).thenReturn(existing);
        DataGenPlan adopted = service.adopt(1L, true);
        assertEquals(DataGenPlan.ST_ADOPTED, adopted.getStatus());
        assertEquals(DataGenPlan.ST_DISCARDED, service.adopt(1L, false).getStatus());
    }
}
