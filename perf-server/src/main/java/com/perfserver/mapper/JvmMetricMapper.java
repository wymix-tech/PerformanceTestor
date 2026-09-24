package com.perfserver.mapper;

import com.perfserver.entity.JvmMetric;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.LocalDateTime;
import java.util.List;

/**
 * JVM 指标 Mapper：批量写入 + 时间窗口查询。
 */
@Mapper
public interface JvmMetricMapper {

    /** 批量插入（agent 上报整批落库） */
    int batchInsert(List<JvmMetric> metrics);

    /** 时间窗口内查询（按时间正序，limit 截断；图表采样用，不做翻页） */
    List<JvmMetric> query(@Param("appName") String appName,
                          @Param("start") LocalDateTime start,
                          @Param("end") LocalDateTime end,
                          @Param("limit") int limit);

    /**
     * 游标分页查询：按 {@code (collect_time, id)} 复合游标倒序翻页。
     *
     * <p>注意与 {@link #query} 的排序方向相反——图表要正序画线（时间从左到右），
     * 列表要倒序看最新。分页必须按"最新在前"取数，故单独一条语句。
     */
    List<JvmMetric> queryByCursor(@Param("appName") String appName,
                                  @Param("start") LocalDateTime start,
                                  @Param("end") LocalDateTime end,
                                  @Param("cursorTime") LocalDateTime cursorTime,
                                  @Param("cursorId") Long cursorId,
                                  @Param("limit") int limit);
}
