package com.perftestor.datagen.endpoint;

import com.perftestor.datagen.model.EndpointRef;
import com.perftestor.datagen.model.SourceFile;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 调用链 BFS 单测：字段注入 / 构造器注入 / 接口多实现 / 熔断四重保护。 */
class CallGraphAnalyzerTest {

    private static final String CTRL = "src/main/java/com/demo/controller/OrderController.java";
    private static final String CTRL_CTOR = "src/main/java/com/demo/controller/CartController.java";
    private static final String SVC = "src/main/java/com/demo/service/OrderService.java";
    private static final String SVC_IMPL = "src/main/java/com/demo/service/impl/OrderServiceImpl.java";
    private static final String SVC_IMPL2 = "src/main/java/com/demo/service/impl/VipOrderServiceImpl.java";
    private static final String USER_SVC = "src/main/java/com/demo/service/UserService.java";
    private static final String USER_SVC_IMPL = "src/main/java/com/demo/service/impl/UserServiceImpl.java";
    private static final String MAPPER = "src/main/java/com/demo/mapper/OrderMapper.java";
    private static final String USER_MAPPER = "src/main/java/com/demo/mapper/UserMapper.java";

    private static SourceFile f(String path, String content) {
        return new SourceFile(path, content);
    }

    /** Controller：字段注入 Service */
    private static final String CONTROLLER = """
            package com.demo.controller;

            import com.demo.service.OrderService;
            import org.springframework.beans.factory.annotation.Autowired;
            import org.springframework.web.bind.annotation.*;

            @RestController
            @RequestMapping("/api/order")
            public class OrderController {

                @Autowired
                private OrderService orderService;

                @GetMapping("/list")
                public Object list(int status) {
                    return orderService.listByStatus(status);
                }

                @GetMapping("/{id}")
                public Object detail(Long id) {
                    return orderService.getById(id);
                }

                @PostMapping("/create")
                public Object create(Object req) {
                    return orderService.create(req);
                }
            }
            """;

    /** Controller：构造器注入 Service */
    private static final String CONTROLLER_CTOR = """
            package com.demo.controller;

            import org.springframework.web.bind.annotation.*;

            @RestController
            @RequestMapping("/api/cart")
            public class CartController {

                private final OrderService orderService;

                public CartController(OrderService orderService) {
                    this.orderService = orderService;
                }

                @GetMapping("/list")
                public Object list() {
                    return orderService.listByStatus(1);
                }
            }
            """;

    private static final String SERVICE = """
            package com.demo.service;

            public interface OrderService {
                Object listByStatus(int status);
                Object getById(Long id);
                Object create(Object req);
            }
            """;

    private static final String SERVICE_IMPL = """
            package com.demo.service.impl;

            import com.demo.service.OrderService;
            import com.demo.service.UserService;
            import com.demo.mapper.OrderMapper;
            import org.springframework.beans.factory.annotation.Autowired;
            import org.springframework.stereotype.Service;

            @Service
            public class OrderServiceImpl implements OrderService {

                @Autowired
                private OrderMapper orderMapper;

                @Autowired
                private UserService userService;

                @Override
                public Object listByStatus(int status) {
                    return orderMapper.selectByStatus(status);
                }

                @Override
                public Object getById(Long id) {
                    userService.touch(1L);
                    return orderMapper.selectById(id);
                }

                @Override
                public Object create(Object req) {
                    userService.touch(2L);
                    return orderMapper.insert(req);
                }
            }
            """;

    private static final String USER_SERVICE = """
            package com.demo.service;

            public interface UserService {
                void touch(Long id);
            }
            """;

    private static final String USER_SERVICE_IMPL = """
            package com.demo.service.impl;

            import com.demo.service.UserService;
            import com.demo.mapper.UserMapper;
            import org.springframework.beans.factory.annotation.Autowired;
            import org.springframework.stereotype.Service;

            @Service
            public class UserServiceImpl implements UserService {

                @Autowired
                private UserMapper userMapper;

                @Override
                public void touch(Long id) {
                    userMapper.updateLastActive(id);
                }
            }
            """;

    private static final String ORDER_MAPPER = """
            package com.demo.mapper;

            public interface OrderMapper {
                Object selectByStatus(int status);
                Object selectById(Long id);
                Object insert(Object req);
            }
            """;

    private static final String USER_MAPPER_SRC = """
            package com.demo.mapper;

            public interface UserMapper {
                void updateLastActive(Long id);
            }
            """;

    private static List<SourceFile> baseFiles() {
        return List.of(
                f(CTRL, CONTROLLER),
                f(SVC, SERVICE),
                f(SVC_IMPL, SERVICE_IMPL),
                f(USER_SVC, USER_SERVICE),
                f(USER_SVC_IMPL, USER_SERVICE_IMPL),
                f(MAPPER, ORDER_MAPPER),
                f(USER_MAPPER, USER_MAPPER_SRC));
    }

    private static EndpointRef entry(String file, String method) {
        return new EndpointRef("GET", "/api/order/list", "OrderController", method, file, 1);
    }

    private static Set<String> invocations(CallGraphAnalyzer.Result r) {
        return r.getInvocations().stream()
                .map(i -> i.getMapperType() + "#" + i.getMethodName())
                .collect(Collectors.toSet());
    }

    @Test
    void fieldInjectionResolvesServiceThenMapper() {
        CallGraphAnalyzer.Result r = new CallGraphAnalyzer().analyze(baseFiles(), entry(CTRL, "list"));
        assertTrue(invocations(r).contains("OrderMapper#selectByStatus"), "实际: " + invocations(r));
        assertFalse(r.isTripped());
    }

    @Test
    void writeEndpointResolvesBothMappersAcrossServices() {
        CallGraphAnalyzer.Result r = new CallGraphAnalyzer().analyze(baseFiles(), entry(CTRL, "create"));
        assertEquals(Set.of("OrderMapper#insert", "UserMapper#updateLastActive"), invocations(r));
    }

    @Test
    void detailEndpointResolvesJoinSideMapper() {
        CallGraphAnalyzer.Result r = new CallGraphAnalyzer().analyze(baseFiles(), entry(CTRL, "detail"));
        assertTrue(invocations(r).contains("UserMapper#updateLastActive"));
    }

    @Test
    void constructorInjectionIsResolved() {
        List<SourceFile> files = new java.util.ArrayList<>(baseFiles());
        files.add(f(CTRL_CTOR, CONTROLLER_CTOR));
        CallGraphAnalyzer.Result r = new CallGraphAnalyzer()
                .analyze(files, new EndpointRef("GET", "/api/cart/list", "CartController", "list", CTRL_CTOR, 1));
        assertTrue(invocations(r).contains("OrderMapper#selectByStatus"), "实际: " + invocations(r));
    }

    @Test
    void multipleImplementationsAreAllIncludedAndFlagged() {
        String secondImpl = SERVICE_IMPL.replace("class OrderServiceImpl", "class VipOrderServiceImpl")
                .replace("OrderMapper", "VipOrderMapper");
        List<SourceFile> files = new java.util.ArrayList<>(baseFiles());
        files.add(f(SVC_IMPL2, secondImpl));
        CallGraphAnalyzer.Result r = new CallGraphAnalyzer().analyze(files, entry(CTRL, "list"));
        assertTrue(r.isMultiImpl(), "接口多实现必须标记，供宿主编排层降置信度");
        assertTrue(invocations(r).contains("OrderMapper#selectByStatus"), "多实现取并集，不能漏表: " + invocations(r));
    }

    @Test
    void mapperIsTerminalNodeNotExpandedFurther() {
        CallGraphAnalyzer.Result r = new CallGraphAnalyzer().analyze(baseFiles(), entry(CTRL, "list"));
        assertTrue(r.getInvocations().stream().noneMatch(i -> i.getMapperType().endsWith("Service")));
    }

    @Test
    void callSiteCarriesFileAndLine() {
        CallGraphAnalyzer.Result r = new CallGraphAnalyzer().analyze(baseFiles(), entry(CTRL, "list"));
        assertFalse(r.getInvocations().isEmpty());
        assertTrue(r.getInvocations().get(0).getCallSite().matches(".+:\\d+"), "证据必须是 文件:行号");
    }

    @Test
    void timeoutDeadlineStopsExpansionAndWarns() {
        CallGraphAnalyzer analyzer = new CallGraphAnalyzer(System.currentTimeMillis() - 1);
        CallGraphAnalyzer.Result r = analyzer.analyze(baseFiles(), entry(CTRL, "list"));
        assertTrue(r.isTripped(), "超时应触发熔断");
        assertTrue(r.getUnresolved().stream().anyMatch(s -> s.contains("超时")), "熔断必须给出原因");
    }

    @Test
    void jdkStaticAndContainerCallsProduceNoUnresolvedNoise() {
        String noisy = """
                package com.demo.service.impl;

                import com.demo.service.OrderService;
                import com.demo.mapper.OrderMapper;
                import org.springframework.beans.factory.annotation.Autowired;
                import org.springframework.stereotype.Service;

                @Service
                public class OrderServiceImpl implements OrderService {

                    @Autowired
                    private OrderMapper orderMapper;

                    @Override
                    public Object listByStatus(int status) {
                        int n = Math.min(Math.max(status, 1), 5000);
                        String id = java.util.UUID.randomUUID().toString();
                        java.util.List<Object> rows = orderMapper.selectByStatus(status);
                        int size = rows.size();
                        boolean eq = "1".equals(id);
                        return orderMapper.selectByStatus(n + size + (eq ? 1 : 0));
                    }
                }
                """;
        List<SourceFile> files = new ArrayList<>(baseFiles());
        files.set(2, f(SVC_IMPL, noisy));
        CallGraphAnalyzer.Result r = new CallGraphAnalyzer().analyze(files, entry(CTRL, "list"));

        assertTrue(invocations(r).contains("OrderMapper#selectByStatus"), "实际: " + invocations(r));
        assertTrue(r.getUnresolved().isEmpty(),
                "Math/UUID/List#size 等与数据库访问无关的调用不得进入 unresolved 通道"
                        + "（会淹没真正需要用户处理的未覆盖项），实际: " + r.getUnresolved());
        assertFalse(r.isTripped());
    }

    @Test
    void missingSourceForBusinessTypeIsReported() {
        // 依赖了二方库里的 Service（无源码）→ 必须提示，否则用户不知道链路在此中断
        String usingExternal = SERVICE_IMPL.replace(
                "private UserService userService;",
                "private ExternalService externalService;")
                .replace("userService.touch(1L);", "externalService.doWork();");
        List<SourceFile> files = new ArrayList<>(baseFiles());
        files.set(2, f(SVC_IMPL, usingExternal));
        // detail 入口才会走到被替换的 touch() 分支
        CallGraphAnalyzer.Result r = new CallGraphAnalyzer().analyze(files, entry(CTRL, "detail"));
        assertTrue(r.getUnresolved().stream().anyMatch(s -> s.contains("ExternalService")),
                "业务类型无源码必须提示，实际: " + r.getUnresolved());
    }

    @Test
    void missingEntryInfoIsReportedNotThrown() {
        CallGraphAnalyzer.Result r = new CallGraphAnalyzer().analyze(baseFiles(), null);
        assertFalse(r.getUnresolved().isEmpty());
        assertTrue(r.getInvocations().isEmpty());
    }

    @Test
    void unparsableSourceDoesNotBreakAnalysis() {
        List<SourceFile> files = new java.util.ArrayList<>(baseFiles());
        files.set(0, f(CTRL, "this is not java {{{"));
        CallGraphAnalyzer.Result r = new CallGraphAnalyzer().analyze(files, entry(CTRL, "list"));
        assertFalse(r.getUnresolved().isEmpty(), "不可解析源码必须给出未覆盖原因，不得静默");
        assertTrue(r.getInvocations().isEmpty());
    }
}
