package com.perfserver.mapper;

import com.perfserver.entity.StressSample;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 压测周期采样 Mapper。
 */
@Mapper
public interface StressSampleMapper {

    int batchInsert(List<StressSample> list);

    /** 按任务查询采样点（时间升序，limit 截断） */
    List<StressSample> queryByTask(@Param("taskId") long taskId, @Param("limit") int limit);
}
