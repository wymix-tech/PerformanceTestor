package com.perfserver.mapper;

import com.perfserver.entity.HotspotMethod;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 方法级 CPU 热点 Mapper。
 */
@Mapper
public interface HotspotMethodMapper {

    /** 批量插入 */
    void batchInsert(List<HotspotMethod> batch);

    /**
     * 查询时间窗内的热点方法（按命中次数降序）。
     * 按 (app_name, method_name, sample_time 粒度) 聚合命中数，取 topN。
     */
    List<HotspotMethod> queryTop(@Param("appName") String appName,
                                 @Param("start") LocalDateTime start,
                                 @Param("end") LocalDateTime end,
                                 @Param("limit") int limit);
}
