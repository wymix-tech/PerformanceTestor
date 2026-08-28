package com.perfdemo.controller;

import com.perfdemo.entity.Order;
import com.perfdemo.service.OrderService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

/**
 * 订单接口。GET /api/order/list、/api/order/stats 为慢 SQL 演示接口。
 */
@RestController
@RequestMapping("/api/order")
public class OrderController {

    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    /** 按状态查订单：无索引全表扫描（慢 SQL 采集演示） */
    @GetMapping("/list")
    public List<Order> listOrders(@RequestParam(defaultValue = "SHIPPED") String status,
                                  @RequestParam(defaultValue = "500") int limit) {
        return orderService.listByStatus(status, Math.min(Math.max(limit, 1), 5000));
    }

    /** 订单状态聚合统计：大表 GROUP BY（慢 SQL 采集演示） */
    @GetMapping("/stats")
    public List<Map<String, Object>> stats() {
        return orderService.stats();
    }

    /** 最近订单 */
    @GetMapping("/recent")
    public List<Order> recent(@RequestParam(defaultValue = "200") int limit) {
        return orderService.recent(Math.min(Math.max(limit, 1), 2000));
    }
}
