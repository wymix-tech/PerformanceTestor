package com.perfserver.mapper;

import com.perfserver.entity.PerformanceIssue;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 性能问题 Mapper。
 */
@Mapper
public interface PerformanceIssueMapper {

    int insert(PerformanceIssue issue);

    /** 分析完成：写入根因并置为 analyzed */
    int updateAnalyzed(@Param("id") long id, @Param("rootCause") String rootCause);

    /** 更新证据 JSON（分析后追加数据库执行计划等） */
    int updateEvidenceJson(@Param("id") long id, @Param("evidenceJson") String evidenceJson);

    /** 补绑源码仓库与定位区间（压测问题分析前置） */
    int updateLocation(@Param("id") long id, @Param("sourceRepoId") Long sourceRepoId,
                       @Param("sourceFileId") Long sourceFileId,
                       @Param("lineStart") Integer lineStart, @Param("lineEnd") Integer lineEnd);

    /** 更新状态（fixed 等） */
    int updateStatus(@Param("id") long id, @Param("status") String status);

    PerformanceIssue findById(@Param("id") long id);

    List<PerformanceIssue> query(@Param("appName") String appName, @Param("issueType") String issueType,
                                 @Param("status") String status, @Param("limit") int limit);
}
