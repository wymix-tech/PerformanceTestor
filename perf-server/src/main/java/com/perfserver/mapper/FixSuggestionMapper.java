package com.perfserver.mapper;

import com.perfserver.entity.FixSuggestion;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 修复方案 Mapper。
 */
@Mapper
public interface FixSuggestionMapper {

    int batchInsert(List<FixSuggestion> list);

    List<FixSuggestion> findByIssue(@Param("issueId") long issueId);

    FixSuggestion findById(@Param("id") long id);

    /** 更新选定状态（同一问题仅一个 selected=1） */
    int updateSelected(@Param("id") long id, @Param("selected") int selected);

    /** 清除该问题下所有方案的选定状态（切换选择前调用，保证单选） */
    int clearSelectedByIssue(@Param("issueId") long issueId);

    /** 写入修复提示词 */
    int updatePromptMd(@Param("id") long id, @Param("promptMd") String promptMd);

    /**
     * 修复回写（P2-2）：记录"方案已落到代码"的来源、时间与验证任务。
     *
     * <p>幂等由 Service 层保证（已回写且来源一致时直接返回既有记录，不重复写）。
     */
    int updateApplied(@Param("id") long id, @Param("appliedAt") LocalDateTime appliedAt,
                      @Param("applyRef") String applyRef, @Param("applyType") String applyType,
                      @Param("verifyTaskId") Long verifyTaskId);
}
