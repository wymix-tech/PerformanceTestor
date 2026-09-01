package com.perfserver.mapper;

import com.perfserver.entity.AiKnowledge;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * AI 知识库 Mapper：结论沉淀与全文检索。
 *
 * <p>检索使用 MySQL FULLTEXT（建表时 WITH PARSER ngram 支持中文），
 * 相关度 score 由 {@code MATCH(title, content) AGAINST(...)} 计算。
 */
@Mapper
public interface AiKnowledgeMapper {

    /** 写入一条结论 */
    void insert(AiKnowledge item);

    /**
     * 全文检索（按相关度降序）。
     *
     * @param query   检索关键词
     * @param appName 应用名过滤（空/null 表示不过滤，全局结论始终参与检索）
     * @param limit   返回条数上限
     */
    List<AiKnowledge> search(@Param("query") String query,
                             @Param("appName") String appName,
                             @Param("limit") int limit);

    /** 最近沉淀的结论（无检索词时兜底展示） */
    List<AiKnowledge> recent(@Param("appName") String appName, @Param("limit") int limit);

    /** 删除单条结论 */
    int deleteById(@Param("id") long id);
}
