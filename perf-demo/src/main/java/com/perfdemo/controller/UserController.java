package com.perfdemo.controller;

import com.perfdemo.entity.User;
import com.perfdemo.service.UserService;
import org.springframework.web.bind.annotation.GetMapping;
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

    private final UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    /**
     * 用户列表（含订单数）：内部 N+1 查询，压测该接口可稳定产出热点方法调用链。
     */
    @GetMapping("/list")
    public List<User> listUsers(@RequestParam(defaultValue = "2000") int limit) {
        return userService.listUsersWithOrderCount(Math.min(Math.max(limit, 1), 10000));
    }
}
