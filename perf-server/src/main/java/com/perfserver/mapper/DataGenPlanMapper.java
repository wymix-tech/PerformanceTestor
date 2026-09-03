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

    /**
     * 只回写分析产出（表清单 + 告警），不动 status/task_id。
     *
     * <p>分析跑在独立线程池，「查对象 → 改字段 → 全字段 update」之间存在时间窗：
     * 期间用户采纳/放弃方案（走 {@link #updateStatus}）或正式灌数回写了
     * {@code task_id}，全字段 update 会用分析启动时查出的旧值把它们冲掉，
     * 且无任何报错——用户会看到「刚采纳又变回待采纳」。</p>
     */
    int updateAnalysis(@Param("id") long id, @Param("tablesJson") String tablesJson,
                       @Param("warningsJson") String warningsJson);

    int deleteById(@Param("id") long id);
}
