package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * AI 性能知识库条目：AI 会话结束沉淀的分析结论/修复经验，支持中文全文检索。
 *
 * <p>检索走 MySQL FULLTEXT（ngram 解析器），score 仅在 {@code MATCH ... AGAINST} 查询时才有值。
 */
public class AiKnowledge {

    private Long id;
    private String appName;
    private String title;
    private String content;
    private String tags;
    private String source;
    private LocalDateTime createTime;
    /** 全文检索相关度（仅 search 查询返回，recent 查询为 0） */
    private Double score;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getAppName() {
        return appName;
    }

    public void setAppName(String appName) {
        this.appName = appName;
    }

    public String getTitle() {
        return title;
    }

    public void setTitle(String title) {
        this.title = title;
    }

    public String getContent() {
        return content;
    }

    public void setContent(String content) {
        this.content = content;
    }

    public String getTags() {
        return tags;
    }

    public void setTags(String tags) {
        this.tags = tags;
    }

    public String getSource() {
        return source;
    }

    public void setSource(String source) {
        this.source = source;
    }

    public LocalDateTime getCreateTime() {
        return createTime;
    }

    public void setCreateTime(LocalDateTime createTime) {
        this.createTime = createTime;
    }

    public Double getScore() {
        return score;
    }

    public void setScore(Double score) {
        this.score = score;
    }
}
