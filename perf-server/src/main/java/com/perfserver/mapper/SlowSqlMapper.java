package com.perfserver.mapper;

import com.perfserver.entity.SlowSql;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 慢 SQL Mapper：批量写入 + 时间窗口/耗时阈值查询。
 */
@Mapper
public interface SlowSqlMapper {

    /** 批量插入（JDBC 拦截与慢日志解析均可走此通道） */
    int batchInsert(List<SlowSql> list);

    /** 查询：应用/时间窗口/最小耗时过滤，按耗时倒序 */
    List<SlowSql> query(@Param("appName") String appName,
                        @Param("start") LocalDateTime start,
                        @Param("end") LocalDateTime end,
                        @Param("minCostMs") Double minCostMs,
                        @Param("limit") int limit);
}
