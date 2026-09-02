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

    /**
     * 只更新告警字段。
     *
     * <p>执行流程中会先回写 task_id/status，若再用全字段 update 覆盖（传入的是执行前查出的
     * 旧对象），会把刚回写的值冲掉。故告警落库走本专用语句。</p>
     */
    int updateWarnings(@Param("id") long id, @Param("warningsJson") String warningsJson);

    int deleteById(@Param("id") long id);
}
