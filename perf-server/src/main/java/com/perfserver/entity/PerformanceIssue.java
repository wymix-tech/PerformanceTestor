package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 性能问题：关联检测证据与源码位置（performance_issue 表）。
 */
public class PerformanceIssue {

    private Long id;
    /** 类型：slow_sql / hot_method / stress_bottleneck / jvm_alert */
    private String issueType;
    private String appName;
    private String title;
    /** 检测证据 JSON（慢 SQL/热点方法/压测数据等） */
    private String evidenceJson;
    private Long sourceRepoId;
    private Long sourceFileId;
    private Integer lineStart;
    private Integer lineEnd;
    /** 大模型根因分析 */
    private String rootCause;
    /** 状态：pending/analyzed/fixed */
    private String status;
    private LocalDateTime createTime;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getIssueType() {
        return issueType;
    }

    public void setIssueType(String issueType) {
        this.issueType = issueType;
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

    public String getEvidenceJson() {
        return evidenceJson;
    }

    public void setEvidenceJson(String evidenceJson) {
        this.evidenceJson = evidenceJson;
    }

    public Long getSourceRepoId() {
        return sourceRepoId;
    }

    public void setSourceRepoId(Long sourceRepoId) {
        this.sourceRepoId = sourceRepoId;
    }

    public Long getSourceFileId() {
        return sourceFileId;
    }

    public void setSourceFileId(Long sourceFileId) {
        this.sourceFileId = sourceFileId;
    }

    public Integer getLineStart() {
        return lineStart;
    }

    public void setLineStart(Integer lineStart) {
        this.lineStart = lineStart;
    }

    public Integer getLineEnd() {
        return lineEnd;
    }

    public void setLineEnd(Integer lineEnd) {
        this.lineEnd = lineEnd;
    }

    public String getRootCause() {
        return rootCause;
    }

    public void setRootCause(String rootCause) {
        this.rootCause = rootCause;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public LocalDateTime getCreateTime() {
        return createTime;
    }

    public void setCreateTime(LocalDateTime createTime) {
        this.createTime = createTime;
    }
}
