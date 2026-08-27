package com.perfdemo.mapper;

import com.perfdemo.entity.User;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

/**
 * 用户数据访问层（注解 SQL）。
 * <p>注意：selectAll 后逐条查询订单数是典型的 N+1 反模式，
 * 用于演示调用链耗时与源码关联分析。</p>
 */
@Mapper
public interface UserMapper {

    @Select("SELECT id, name, age, email, created_at AS createdAt FROM perf_user ORDER BY id LIMIT #{limit}")
    List<User> selectAll(@Param("limit") int limit);

    @Select("SELECT COUNT(*) FROM perf_order WHERE user_id = #{userId}")
    long countOrders(@Param("userId") long userId);

    @Select("SELECT id, name, age, email, created_at AS createdAt FROM perf_user WHERE id = #{id}")
    User selectById(@Param("id") long id);
}
