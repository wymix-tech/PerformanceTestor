package com.perfserver.mapper;

import com.perfserver.entity.DataGenPlan;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 接口驱动数据方案 Mapper：方案落库、回看列表、状态流转与任务回写。
 */
@Mapper
public interface DataGenPlanMapper {

    int insert(DataGenPlan plan);

    int update(DataGenPlan plan);

    DataGenPlan findById(@Param("id") long id);

    /** 方案列表：按仓库 + 时间倒序 */
    List<DataGenPlan> query(@Param("repoId") Long repoId, @Param("status") String status, @Param("limit") int limit);

    int updateStatus(@Param("id") long id, @Param("status") String status);

    /** 回写关联灌数任务 ID（方案 → 任务，便于追溯） */
    int updateTaskId(@Param("id") long id, @Param("taskId") Long taskId);

    int deleteById(@Param("id") long id);
}
