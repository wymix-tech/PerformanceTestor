package com.perfserver.mapper;

import com.perfserver.entity.TraceChain;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 调用链 Mapper：写入 + 列表查询 + 详情查询。
 */
@Mapper
public interface TraceChainMapper {

    int insert(TraceChain chain);

    /** 列表查询：应用/根方法关键字/时间窗口过滤，按时间倒序 */
    List<TraceChain> query(@Param("appName") String appName,
                           @Param("method") String method,
                           @Param("start") LocalDateTime start,
                           @Param("end") LocalDateTime end,
                           @Param("limit") int limit);

    /** 按 traceId 查询详情 */
    TraceChain findByTraceId(@Param("traceId") String traceId);
}
