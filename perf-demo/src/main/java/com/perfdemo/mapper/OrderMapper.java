package com.perfdemo.mapper;

import com.perfdemo.entity.Order;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;
import java.util.Map;

/**
 * 订单数据访问层（注解 SQL）。
 * <p>注意：status 列无索引，selectByStatus 为全表扫描（慢 SQL 演示）；
 * selectStats 对全表做聚合（大表聚合慢查询演示）。</p>
 */
@Mapper
public interface OrderMapper {

    /** 按状态过滤：perf_order.status 无索引，命中全表扫描 */
    @Select("SELECT id, user_id AS userId, amount, status, remark, created_at AS createdAt "
            + "FROM perf_order WHERE status = #{status} ORDER BY id DESC LIMIT #{limit}")
    List<Order> selectByStatus(@Param("status") String status, @Param("limit") int limit);

    /** 全表聚合：无索引列 GROUP BY + COUNT，大表耗时明显 */
    @Select("SELECT status, COUNT(*) AS cnt, ROUND(AVG(amount), 2) AS avgAmount "
            + "FROM perf_order GROUP BY status ORDER BY cnt DESC")
    List<Map<String, Object>> selectStats();

    /** 最近订单（按时间倒序，无覆盖索引） */
    @Select("SELECT id, user_id AS userId, amount, status, remark, created_at AS createdAt "
            + "FROM perf_order ORDER BY created_at DESC LIMIT #{limit}")
    List<Order> selectRecent(@Param("limit") int limit);
}
