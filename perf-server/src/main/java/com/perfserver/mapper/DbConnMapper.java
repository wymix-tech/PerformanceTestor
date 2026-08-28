package com.perfserver.mapper;

import com.perfserver.entity.DbConn;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 数据库连接配置 Mapper。
 */
@Mapper
public interface DbConnMapper {

    int insert(DbConn conn);

    List<DbConn> list(@Param("limit") int limit);

    DbConn findById(@Param("id") long id);

    int deleteById(@Param("id") long id);

    /** 唯一默认连接：先清空再设置 */
    int clearDefault();

    int setDefault(@Param("id") long id);

    /** 匹配连接：host 精确优先，其次默认，再次第一条 */
    List<DbConn> matchOrder();
}
