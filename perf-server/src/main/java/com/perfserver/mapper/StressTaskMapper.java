package com.perfserver.mapper;

import com.perfserver.entity.StressTask;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 压测任务 Mapper。
 */
@Mapper
public interface StressTaskMapper {

    int insert(StressTask task);

    /** 更新状态（running） */
    int updateStatus(@Param("id") long id, @Param("status") String status);

    /** 更新结果并置为 finished */
    int updateFinished(@Param("id") long id, @Param("resultJson") String resultJson,
                       @Param("finishTime") LocalDateTime finishTime);

    /** 更新为 failed */
    int updateFailed(@Param("id") long id, @Param("errorMsg") String errorMsg);

    /** 更新结果 JSON（容量评估等后续追加字段） */
    int updateResultJson(@Param("id") long id, @Param("resultJson") String resultJson);

    StressTask findById(@Param("id") long id);

    /** 任务列表：按状态过滤（空查全部），时间倒序 */
    List<StressTask> query(@Param("status") String status, @Param("limit") int limit);

    /**
     * 同场景历史任务：按应用名 + 目标 URL + 状态过滤，时间倒序。
     *
     * <p>过滤下推到 SQL，避免把全量任务拉到内存再筛——任务表会随使用持续增长。
     */
    List<StressTask> queryByScene(@Param("appName") String appName,
                                  @Param("targetUrl") String targetUrl,
                                  @Param("status") String status,
                                  @Param("limit") int limit);
}
