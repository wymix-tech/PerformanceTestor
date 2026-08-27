package com.perfdemo.service;

import com.perfdemo.entity.Order;
import com.perfdemo.mapper.OrderMapper;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Map;

/**
 * 订单业务服务。
 */
@Service
public class OrderService {

    private final OrderMapper orderMapper;

    public OrderService(OrderMapper orderMapper) {
        this.orderMapper = orderMapper;
    }

    /** 按状态查询：无索引列过滤，全表扫描（慢 SQL 演示） */
    public List<Order> listByStatus(String status, int limit) {
        return orderMapper.selectByStatus(status, limit);
    }

    /** 全表聚合统计：大表 GROUP BY，耗时明显（慢 SQL 演示） */
    public List<Map<String, Object>> stats() {
        return orderMapper.selectStats();
    }

    /** 最近订单 */
    public List<Order> recent(int limit) {
        return orderMapper.selectRecent(limit);
    }
}
