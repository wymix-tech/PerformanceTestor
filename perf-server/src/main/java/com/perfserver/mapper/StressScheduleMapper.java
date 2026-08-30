package com.perfserver.mapper;

import com.perfserver.entity.StressSchedule;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 定时压测调度 Mapper。
 */
@Mapper
public interface StressScheduleMapper {

    void insert(StressSchedule schedule);

    void update(StressSchedule schedule);

    void deleteById(@Param("id") long id);

    void updateEnabled(@Param("id") long id, @Param("enabled") int enabled);

    /** 到期任务（启用中且 next_run_time 已到） */
    List<StressSchedule> findDue(@Param("now") LocalDateTime now, @Param("limit") int limit);

    /** 触发后更新运行状态（lastTaskId/lastRunTime/nextRunTime 原子推进，防重复触发）；返回更新行数 */
    int updateRunState(@Param("id") long id,
                       @Param("lastTaskId") long lastTaskId,
                       @Param("lastRunTime") LocalDateTime lastRunTime,
                       @Param("nextRunTime") LocalDateTime nextRunTime);

    void clearLastTask(@Param("id") long id);

    StressSchedule findById(@Param("id") long id);

    List<StressSchedule> listAll();
}
