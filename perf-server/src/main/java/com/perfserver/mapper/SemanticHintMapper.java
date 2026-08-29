package com.perfserver.mapper;

import com.perfserver.entity.SemanticHint;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 语义提示 Mapper：魔法值候选与关联建议的幂等落库与查询。
 */
@Mapper
public interface SemanticHintMapper {

    /** 幂等写入（repo+type+表+列+父表唯一；重复时更新值集与来源） */
    int upsert(SemanticHint hint);

    List<SemanticHint> query(@Param("repoId") Long repoId, @Param("tableName") String tableName,
                             @Param("hintType") String hintType, @Param("status") String status,
                             @Param("limit") int limit);

    SemanticHint findById(@Param("id") long id);

    int updateStatus(@Param("id") long id, @Param("status") String status);

    /** 按仓库清空（重扫前） */
    int deleteByRepo(@Param("repoId") long repoId);
}

