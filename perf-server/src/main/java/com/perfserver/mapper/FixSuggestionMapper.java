package com.perfserver.mapper;

import com.perfserver.entity.FixSuggestion;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

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
}
