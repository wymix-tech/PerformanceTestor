package com.perfdemo.service;

import com.perfdemo.entity.User;
import com.perfdemo.mapper.UserMapper;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;

/**
 * 用户业务服务。
 * <p>listUsersWithOrderCount 在拿到用户列表后，对每个用户逐条调用
 * countOrders（N+1 次查询）。当用户量大时该接口耗时显著，是
 * perf-demo 中最典型的可分析性能问题。</p>
 */
@Service
public class UserService {

    private final UserMapper userMapper;

    public UserService(UserMapper userMapper) {
        this.userMapper = userMapper;
    }

    /**
     * 返回用户列表并附加每人订单数。
     * 反模式：循环内逐条查库（N+1），正确做法是
     * SELECT user_id, COUNT(*) ... GROUP BY user_id 一次查出。
     */
    public List<User> listUsersWithOrderCount(int limit) {
        List<User> users = userMapper.selectAll(limit);
        List<User> result = new ArrayList<>(users.size());
        for (User user : users) {
            // N+1：对每个用户单独执行一次 COUNT 查询
            long orderCount = userMapper.countOrders(user.getId());
            user.setEmail(user.getEmail() + "（订单数 " + orderCount + "）");
            result.add(user);
        }
        return result;
    }
}
