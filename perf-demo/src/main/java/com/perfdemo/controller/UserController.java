package com.perfdemo.controller;

import com.perfdemo.entity.User;
import com.perfdemo.service.UserService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 用户接口。GET /api/user/list 为 N+1 查询热点接口。
 */
@RestController
@RequestMapping("/api/user")
public class UserController {

    private static final Logger log = LoggerFactory.getLogger(UserController.class);

    /** 平台压测流量标记头（与 perf-server HttpStressEngine/JmeterStressEngine 注入一致） */
    static final String TRAFFIC_MARK_HEADER = "X-Perf-Test";

    private final UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    /**
     * 用户列表（含订单数）：内部 N+1 查询，压测该接口可稳定产出热点方法调用链。
     *
     * <p>压测流量隔离演示：携带 {@code X-Perf-Test: 1} 的请求走影子数据源路由
     * （演示环境以响应头 + 日志标记替代真实影子表切换，生产可在此切只读库/影子表）。
     */
    @GetMapping("/list")
    public List<User> listUsers(@RequestParam(defaultValue = "2000") int limit,
                                @RequestHeader(value = TRAFFIC_MARK_HEADER, required = false) String perfTestMark) {
        boolean isPerfTest = "1".equals(perfTestMark);
        if (isPerfTest) {
            log.debug("压测流量标记命中：本请求走影子路由（生产环境此处切换影子表/只读库）");
        }
        return userService.listUsersWithOrderCount(Math.min(Math.max(limit, 1), 10000));
    }
}
