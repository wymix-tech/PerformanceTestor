package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.ObjectMapper;
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
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

/**
 * 外键引用闭环验收：JOIN / N+1 → ref/refSkew 规则 → 父表自动补齐。
 *
 * <p>PR 评审发现该链路是死代码（{@code contexts} 恒空、{@code refRule()} 无调用点），
 * 本测试守住修复后的完整闭环：SQL → 外键边 → 规则 JSON → 父表入清单。</p>
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class JoinRefE2ETest {

    private static final String CTRL = "src/main/java/com/demo/OrderController.java";
    private static final String SVC = "src/main/java/com/demo/OrderService.java";
    private static final String MAPPER = "src/main/java/com/demo/mapper/OrderMapper.java";
    private static final String USER_MAPPER = "src/main/java/com/demo/mapper/UserMapper.java";

    /** JOIN 查询：perf_order o join perf_user u on o.user_id = u.id */
    private static final String CONTROLLER = """
            package com.demo;

            import org.springframework.beans.factory.annotation.Autowired;
            import org.springframework.web.bind.annotation.*;

            @RestController
            @RequestMapping("/api/order")
            public class OrderController {

                @Autowired
                private OrderService orderService;

                @GetMapping("/list")
                public Object list() {
                    return orderService.listWithUser();
                }

                @GetMapping("/{id}")
                public Object detail(Long id) {
                    return orderService.detailWithUser(id);
                }

                @GetMapping("/count")
                public Object count(String userId) {
                    return orderService.countByUser(userId);
                }

                // 真正的 N+1：先查用户，再在循环里逐个统计订单
                @GetMapping("/n1")
                public Object n1() {
                    return orderService.n1List();
                }
            }
            """;

    private static final String SERVICE = """
            package com.demo;

            import org.springframework.beans.factory.annotation.Autowired;
            import org.springframework.stereotype.Service;
            import com.demo.mapper.OrderMapper;
            import com.demo.mapper.UserMapper;

            @Service
            public class OrderService {

                @Autowired
                private OrderMapper orderMapper;

                @Autowired
                private UserMapper userMapper;

                public Object listWithUser() {
                    return orderMapper.selectWithUser();
                }

                public Object detailWithUser(Long id) {
                    return orderMapper.selectWithUserById(id);
                }

                public Object countByUser(String userId) {
                    return orderMapper.countByUser(userId);
                }

                // N+1：users 来自 perf_user，循环内 countByUser 走 perf_order
                public Object n1List() {
                    for (Object u : userMapper.selectAll()) {
                        orderMapper.countByUser(String.valueOf(u));
                    }
                    return null;
                }
            }
            """;

    private static final String ORDER_MAPPER = """
            package com.demo.mapper;

            import org.apache.ibatis.annotations.Select;

            public interface OrderMapper {
                @Select("SELECT o.id, u.name FROM perf_order o JOIN perf_user u ON o.user_id = u.id "
                        + "WHERE o.status = 1 LIMIT 20")
                Object selectWithUser();

                @Select("SELECT o.id FROM perf_order o JOIN perf_user u ON o.user_id = u.id WHERE o.id = #{id}")
                Object selectWithUserById(Long id);

                // N+1 形态：无 JOIN，但 WHERE 里出现 user_id
                @Select("SELECT COUNT(*) FROM perf_order WHERE user_id = #{userId} AND status = 1")
                Object countByUser(String userId);
            }
            """;

    private static final String USER_MAPPER_SRC = """
            package com.demo.mapper;

            import org.apache.ibatis.annotations.Select;

            public interface UserMapper {
                @Select("SELECT id, name FROM perf_user WHERE status = 1 LIMIT 20")
                Object selectAll();
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
        service = new EndpointDataPlanService(planMapper, repoMapper, sourceFileProvider, mapper,
                new RtBaselineResolver(taskMapper, sampleMapper, mapper),
                new PlanRuleComposer(), new LlmPlanEnhancer(null, mapper));
        when(sourceFileProvider.loadFiles(anyLong(), anyInt())).thenReturn(List.of(
                new SourceFile(CTRL, CONTROLLER),
                new SourceFile(SVC, SERVICE),
                new SourceFile(MAPPER, ORDER_MAPPER),
                new SourceFile(USER_MAPPER, USER_MAPPER_SRC)));
        when(sourceFileProvider.countIndexed(anyLong())).thenReturn(4);
        SourceRepo repo = new SourceRepo();
        repo.setAppName("demo");
        when(repoMapper.findById(anyLong())).thenReturn(repo);
        when(taskMapper.queryByScene(anyString(), anyString(), anyString(), anyInt())).thenReturn(List.of());
    }

    private static EndpointDataPlanService.AnalyzeRequest req(String method, String path) {
        EndpointDataPlanService.AnalyzeRequest r = new EndpointDataPlanService.AnalyzeRequest();
        r.setRepoId(1L);
        r.setDbUrl("jdbc:mysql://127.0.0.1:3306/demo");
        EndpointDataPlanService.EndpointInput in = new EndpointDataPlanService.EndpointInput();
        in.setHttpMethod(method);
        in.setPath(path);
        r.getEndpoints().add(in);
        RowCountEstimator.StressParams s = new RowCountEstimator.StressParams();
        s.setConcurrency(50);
        s.setDurationSec(120);
        r.setStress(s);
        return r;
    }

    private Map<String, PlanIntegrityChecker.PlanTable> tables(String path) {
        List<String> warnings = new ArrayList<>();
        Map<String, PlanIntegrityChecker.PlanTable> m = new java.util.LinkedHashMap<>();
        for (PlanIntegrityChecker.PlanTable t : service.doAnalyze(req("GET", path), warnings)) {
            m.put(t.getTable(), t);
        }
        return m;
    }

    private List<String> warnings(String path) {
        List<String> w = new ArrayList<>();
        service.doAnalyze(req("GET", path), w);
        return w;
    }

    @Test
    void joinQueryGeneratesRefRuleOnChildTable() {
        var t = tables("/api/order/list");
        assertTrue(t.containsKey("perf_order"), "实际: " + t.keySet());
        String fields = t.get("perf_order").getFields();
        assertTrue(fields != null && fields.contains("\"type\":\"ref\""),
                "JOIN 外键列必须生成 ref 规则，实际: " + fields);
        assertTrue(fields.contains("\"table\":\"perf_user\""), "实际: " + fields);
    }

    @Test
    void joinQueryCompletesParentTableIntoPlan() {
        var t = tables("/api/order/list");
        // perf_user 只出现在 JOIN ON 中，若不补进清单，引擎会静默丢弃 explicit 边
        assertTrue(t.containsKey("perf_user"),
                "JOIN 的父表必须进入方案清单，否则 ref 池为空且无任何报错，实际: " + t.keySet());
        assertEquals("READ", t.get("perf_user").getAccess());
    }

    @Test
    void refTargetsAreAlwaysPresentInFinalTableList() {
        // 核心不变量：任何 ref 指向的父表都必须在最终表清单里，
        // 否则引擎会静默丢弃 explicit 边（ref 池为空、数据退化成随机数且无任何报错）
        for (String path : List.of("/api/order/list", "/api/order/{id}", "/api/order/count")) {
            var t = tables(path);
            PlanIntegrityChecker checker = new PlanIntegrityChecker(new ObjectMapper());
            for (PlanIntegrityChecker.PlanTable pt : t.values()) {
                for (String parent : checker.refTargets(pt.getFields())) {
                    assertTrue(t.containsKey(parent),
                            "接口 " + path + " 的 ref 指向 " + parent + "，但该表不在方案清单中: " + t.keySet());
                }
            }
        }
    }

    @Test
    void parentFromJoinNeedsNoCompletionWarning() {
        // 父表已由 JOIN 子句直接提取出来时，不需要再补（也就没有补表告警）
        assertFalse(warnings("/api/order/list").stream()
                        .anyMatch(w -> w.contains("已自动补入")),
                "父表已在清单内时不应重复补入，实际: " + warnings("/api/order/list"));
    }

    @Test
    void detailQueryGeneratesRefSkewWithHotSpot() {
        var t = tables("/api/order/{id}");
        String fields = t.get("perf_order").getFields();
        assertTrue(fields != null && fields.contains("refSkew"),
                "详情类访问应生成热点倾斜规则，实际: " + fields);
        assertTrue(fields.contains("hotPercent"), "实际: " + fields);
    }

    @Test
    void n1QueryWithoutJoinStillResolvesFkByNaming() {
        // N+1：perf_user 与 perf_order 在不同 SQL 中，靠列名 user_id 建立外键
        var t = tables("/api/order/n1");
        assertTrue(t.containsKey("perf_user") && t.containsKey("perf_order"), "实际: " + t.keySet());
        String fields = t.get("perf_order").getFields();
        assertTrue(fields != null && fields.contains("\"type\":\"ref\""),
                "N+1 形态（无 JOIN）也应识别外键，实际: " + fields);
        assertTrue(fields.contains("\"table\":\"perf_user\""), "实际: " + fields);
    }

    @Test
    void singleTableWithoutKnownParentYieldsNoRef() {
        // 只有 perf_order、方案里没有 perf_user 时，不应凭空造出父表
        var t = tables("/api/order/count");
        assertTrue(t.containsKey("perf_order"), "实际: " + t.keySet());
        assertFalse(t.containsKey("perf_user"), "不得凭空推断出不存在的关联表");
    }

    @Test
    void refRulesSurviveJsonRoundTripForExecution() throws Exception {
        // 父表补齐与规则下发都依赖 fields JSON 能被重新解析
        var t = tables("/api/order/list");
        String json = new ObjectMapper().writeValueAsString(new ArrayList<>(t.values()));
        List<PlanIntegrityChecker.PlanTable> parsed = service.parseTables(json);
        assertFalse(parsed.isEmpty());
        assertTrue(parsed.stream().anyMatch(p -> p.getFields() != null
                && p.getFields().contains("\"ref\"")));
    }

    @Test
    void detailEndpointDetection() {
        assertTrue(EndpointDataPlanService.isDetailEndpoint("/api/order/{id}"));
        assertTrue(EndpointDataPlanService.isDetailEndpoint("/api/order/{orderId}"));
        assertFalse(EndpointDataPlanService.isDetailEndpoint("/api/order/list"));
        assertFalse(EndpointDataPlanService.isDetailEndpoint(""));
        assertFalse(EndpointDataPlanService.isDetailEndpoint(null));
    }
}
