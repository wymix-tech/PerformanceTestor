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

    /**
     * 批量写入（语义与 {@link #insert} 一致：{@code trace_id} 冲突时更新，保持幂等重放）。
     *
     * <p>单批 500 条逐条 INSERT 会产生 500 次网络往返与自增锁竞争，是上报写入的吞吐瓶颈；
     * 批量版把往返降到 1 次（调用方按 {@code INSERT_CHUNK} 分片防 {@code max_allowed_packet} 超限）。
     */
    int batchInsert(@Param("list") List<TraceChain> list);

    /** 列表查询：应用/根方法关键字/时间窗口过滤，按时间倒序 */
    List<TraceChain> query(@Param("appName") String appName,
                           @Param("method") String method,
                           @Param("start") LocalDateTime start,
                           @Param("end") LocalDateTime end,
                           @Param("limit") int limit);

    /** 按 traceId 查询详情 */
    TraceChain findByTraceId(@Param("traceId") String traceId);
}
