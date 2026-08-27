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

    /** 时间窗口内查询（按时间倒序，limit 截断） */
    List<JvmMetric> query(@Param("appName") String appName,
                          @Param("start") LocalDateTime start,
                          @Param("end") LocalDateTime end,
                          @Param("limit") int limit);

    /** 查询最近 N 条（不限时间窗口） */
    List<JvmMetric> queryLatest(@Param("appName") String appName, @Param("limit") int limit);
}
