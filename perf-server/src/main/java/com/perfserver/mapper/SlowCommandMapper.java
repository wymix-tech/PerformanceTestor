package com.perfserver.mapper;

import com.perfserver.entity.SlowCommand;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 中间件慢命令 Mapper：批量写入 + 应用/类型/时间窗口/耗时阈值查询。
 */
@Mapper
public interface SlowCommandMapper {

    /** 批量插入（agent slowCommand 通道） */
    int batchInsert(List<SlowCommand> list);

    /**
     * 查询：应用/类型/时间窗口/最小耗时过滤，按耗时倒序。
     *
     * @param type 中间件类型（redis/es/mq），null 或空表示全部
     */
    List<SlowCommand> query(@Param("appName") String appName,
                            @Param("type") String type,
                            @Param("start") LocalDateTime start,
                            @Param("end") LocalDateTime end,
                            @Param("minCostMs") Double minCostMs,
                            @Param("limit") int limit);

    /** 游标分页查询：按 {@code (execute_time, id)} 复合游标倒序翻页（理由同 SlowSqlMapper） */
    List<SlowCommand> queryByCursor(@Param("appName") String appName,
                                    @Param("type") String type,
                                    @Param("start") LocalDateTime start,
                                    @Param("end") LocalDateTime end,
                                    @Param("minCostMs") Double minCostMs,
                                    @Param("cursorTime") LocalDateTime cursorTime,
                                    @Param("cursorId") Long cursorId,
                                    @Param("limit") int limit);

    /** 按关联调用链 ID 查询（调用链详情联动） */
    List<SlowCommand> findByTraceId(@Param("traceId") String traceId);
}
