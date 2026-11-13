"""调用链 BFS 等价单测：移植 CallGraphAnalyzerTest 全部 12 个用例。

字段注入 / 构造器注入 / 接口多实现 / 熔断四重保护。
"""

import time

from datagen.call_graph_analyzer import CallGraphAnalyzer
from datagen.model import EndpointRef, SourceFile

CTRL = "src/main/java/com/demo/controller/OrderController.java"
CTRL_CTOR = "src/main/java/com/demo/controller/CartController.java"
SVC = "src/main/java/com/demo/service/OrderService.java"
SVC_IMPL = "src/main/java/com/demo/service/impl/OrderServiceImpl.java"
SVC_IMPL2 = "src/main/java/com/demo/service/impl/VipOrderServiceImpl.java"
USER_SVC = "src/main/java/com/demo/service/UserService.java"
USER_SVC_IMPL = "src/main/java/com/demo/service/impl/UserServiceImpl.java"
MAPPER = "src/main/java/com/demo/mapper/OrderMapper.java"
USER_MAPPER = "src/main/java/com/demo/mapper/UserMapper.java"

CONTROLLER = """
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
"""

CONTROLLER_CTOR = """
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
"""

SERVICE = """
package com.demo.service;

public interface OrderService {
    Object listByStatus(int status);
    Object getById(Long id);
    Object create(Object req);
}
"""

SERVICE_IMPL = """
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
"""

USER_SERVICE = """
package com.demo.service;

public interface UserService {
    void touch(Long id);
}
"""

USER_SERVICE_IMPL = """
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
"""

ORDER_MAPPER = """
package com.demo.mapper;

public interface OrderMapper {
    Object selectByStatus(int status);
    Object selectById(Long id);
    Object insert(Object req);
}
"""

USER_MAPPER_SRC = """
package com.demo.mapper;

public interface UserMapper {
    void updateLastActive(Long id);
}
"""


def base_files() -> list[SourceFile]:
    return [
        SourceFile(CTRL, CONTROLLER),
        SourceFile(SVC, SERVICE),
        SourceFile(SVC_IMPL, SERVICE_IMPL),
        SourceFile(USER_SVC, USER_SERVICE),
        SourceFile(USER_SVC_IMPL, USER_SERVICE_IMPL),
        SourceFile(MAPPER, ORDER_MAPPER),
        SourceFile(USER_MAPPER, USER_MAPPER_SRC),
    ]


def entry(file: str, method: str) -> EndpointRef:
    return EndpointRef("GET", "/api/order/list", "OrderController", method, file, 1)


def invocations(r) -> set[str]:
    return {f"{i.mapper_type}#{i.method_name}" for i in r.invocations}


def test_field_injection_resolves_service_then_mapper():
    r = CallGraphAnalyzer().analyze(base_files(), entry(CTRL, "list"))
    assert "OrderMapper#selectByStatus" in invocations(r), f"实际: {invocations(r)}"
    assert not r.tripped


def test_write_endpoint_resolves_both_mappers_across_services():
    r = CallGraphAnalyzer().analyze(base_files(), entry(CTRL, "create"))
    assert invocations(r) == {"OrderMapper#insert", "UserMapper#updateLastActive"}


def test_detail_endpoint_resolves_join_side_mapper():
    r = CallGraphAnalyzer().analyze(base_files(), entry(CTRL, "detail"))
    assert "UserMapper#updateLastActive" in invocations(r)


def test_constructor_injection_is_resolved():
    files = base_files() + [SourceFile(CTRL_CTOR, CONTROLLER_CTOR)]
    r = CallGraphAnalyzer().analyze(
        files, EndpointRef("GET", "/api/cart/list", "CartController", "list", CTRL_CTOR, 1))
    assert "OrderMapper#selectByStatus" in invocations(r), f"实际: {invocations(r)}"


def test_multiple_implementations_are_all_included_and_flagged():
    second_impl = (SERVICE_IMPL.replace("class OrderServiceImpl", "class VipOrderServiceImpl")
                   .replace("OrderMapper", "VipOrderMapper"))
    files = base_files() + [SourceFile(SVC_IMPL2, second_impl)]
    r = CallGraphAnalyzer().analyze(files, entry(CTRL, "list"))
    assert r.multi_impl, "接口多实现必须标记，供宿主编排层降置信度"
    assert "OrderMapper#selectByStatus" in invocations(r), \
        f"多实现取并集，不能漏表: {invocations(r)}"


def test_mapper_is_terminal_node_not_expanded_further():
    r = CallGraphAnalyzer().analyze(base_files(), entry(CTRL, "list"))
    assert not any(i.mapper_type.endswith("Service") for i in r.invocations)


def test_call_site_carries_file_and_line():
    r = CallGraphAnalyzer().analyze(base_files(), entry(CTRL, "list"))
    assert r.invocations
    assert r.invocations[0].call_site.rsplit(":", 1)[1].isdigit(), "证据必须是 文件:行号"


def test_timeout_deadline_stops_expansion_and_warns():
    analyzer = CallGraphAnalyzer(deadline_ms=time.monotonic() * 1000 - 1)
    r = analyzer.analyze(base_files(), entry(CTRL, "list"))
    assert r.tripped, "超时应触发熔断"
    assert any("超时" in s for s in r.unresolved), "熔断必须给出原因"


def test_jdk_static_and_container_calls_produce_no_unresolved_noise():
    noisy = """
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
    """
    files = base_files()
    files[2] = SourceFile(SVC_IMPL, noisy)
    r = CallGraphAnalyzer().analyze(files, entry(CTRL, "list"))

    assert "OrderMapper#selectByStatus" in invocations(r), f"实际: {invocations(r)}"
    assert not r.unresolved, (
        "Math/UUID/List#size 等与数据库访问无关的调用不得进入 unresolved 通道"
        f"（会淹没真正需要用户处理的未覆盖项），实际: {r.unresolved}")
    assert not r.tripped


def test_missing_source_for_business_type_is_reported():
    # 依赖了二方库里的 Service（无源码）→ 必须提示，否则用户不知道链路在此中断
    using_external = (SERVICE_IMPL
                      .replace("private UserService userService;",
                               "private ExternalService externalService;")
                      .replace("userService.touch(1L);", "externalService.doWork();"))
    files = base_files()
    files[2] = SourceFile(SVC_IMPL, using_external)
    # detail 入口才会走到被替换的 touch() 分支
    r = CallGraphAnalyzer().analyze(files, entry(CTRL, "detail"))
    assert any("ExternalService" in s for s in r.unresolved), \
        f"业务类型无源码必须提示，实际: {r.unresolved}"


def test_missing_entry_info_is_reported_not_thrown():
    r = CallGraphAnalyzer().analyze(base_files(), None)
    assert r.unresolved
    assert not r.invocations


def test_unparsable_source_does_not_break_analysis():
    files = base_files()
    files[0] = SourceFile(CTRL, "this is not java {{{")
    r = CallGraphAnalyzer().analyze(files, entry(CTRL, "list"))
    assert r.unresolved, "不可解析源码必须给出未覆盖原因，不得静默"
    assert not r.invocations
