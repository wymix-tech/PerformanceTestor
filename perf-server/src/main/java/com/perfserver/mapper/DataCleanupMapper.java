package com.perfserver.mapper;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 项目历史数据清理：按应用（app_name）批量删除采集/派生数据。
 * 项目本身（source_repo / source_file）不在清理范围。
 */
@Mapper
public interface DataCleanupMapper {

    /** 调用链 */
    int deleteTraceByApp(@Param("appName") String appName);

    /** JVM 指标 */
    int deleteJvmByApp(@Param("appName") String appName);

    /** 慢 SQL */
    int deleteSlowSqlByApp(@Param("appName") String appName);

    /** 压测任务 ID 列表（供级联删采样） */
    List<Long> findStressTaskIdsByApp(@Param("appName") String appName);

    /** 压测采样（按任务 ID 级联删除） */
    int deleteStressSamplesByTaskIds(@Param("taskIds") List<Long> taskIds);

    /** 压测任务 */
    int deleteStressTasksByApp(@Param("appName") String appName);

    /** 性能问题 ID 列表（供级联删方案） */
    List<Long> findIssueIdsByApp(@Param("appName") String appName);

    /** 修复方案（按问题 ID 级联删除） */
    int deleteFixByIssueIds(@Param("issueIds") List<Long> issueIds);

    /** 性能问题 */
    int deleteIssuesByApp(@Param("appName") String appName);

    /** 应用实例心跳 */
    int deleteAppInstancesByApp(@Param("appName") String appName);

    /** 数据库连接配置 */
    int deleteDbConnsByApp(@Param("appName") String appName);
}
