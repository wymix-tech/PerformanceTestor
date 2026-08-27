package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 修复方案：每个问题 2-3 条，用户选定后生成修复提示词（fix_suggestion 表）。
 */
public class FixSuggestion {

    private Long id;
    private Long issueId;
    private String title;
    /** 涉及文件 JSON：[{"file":"...","action":"..."}] */
    private String filesJson;
    /** 修改点 JSON：[{"point":"..."}] */
    private String changesJson;
    /** 代码改动要点 JSON：[{"file":"...","code":"..."}] */
    private String codeChangesJson;
    private String benefit;
    private String risk;
    /** 修复提示词 Markdown（选定后生成） */
    private String promptMd;
    /** 是否被选定：1 是 0 否 */
    private Integer selected;
    private LocalDateTime createTime;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public Long getIssueId() {
        return issueId;
    }

    public void setIssueId(Long issueId) {
        this.issueId = issueId;
    }

    public String getTitle() {
        return title;
    }

    public void setTitle(String title) {
        this.title = title;
    }

    public String getFilesJson() {
        return filesJson;
    }

    public void setFilesJson(String filesJson) {
        this.filesJson = filesJson;
    }

    public String getChangesJson() {
        return changesJson;
    }

    public void setChangesJson(String changesJson) {
        this.changesJson = changesJson;
    }

    public String getCodeChangesJson() {
        return codeChangesJson;
    }

    public void setCodeChangesJson(String codeChangesJson) {
        this.codeChangesJson = codeChangesJson;
    }

    public String getBenefit() {
        return benefit;
    }

    public void setBenefit(String benefit) {
        this.benefit = benefit;
    }

    public String getRisk() {
        return risk;
    }

    public void setRisk(String risk) {
        this.risk = risk;
    }

    public String getPromptMd() {
        return promptMd;
    }

    public void setPromptMd(String promptMd) {
        this.promptMd = promptMd;
    }

    public Integer getSelected() {
        return selected;
    }

    public void setSelected(Integer selected) {
        this.selected = selected;
    }

    public LocalDateTime getCreateTime() {
        return createTime;
    }

    public void setCreateTime(LocalDateTime createTime) {
        this.createTime = createTime;
    }
}
