package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 源码仓库：zip/git/本地目录导入记录（source_repo 表）。
 */
public class SourceRepo {

    private Long id;
    private String repoName;
    /** 类型：zip / git / local */
    private String repoType;
    /** 磁盘根路径或 git url 或本地路径 */
    private String sourcePath;
    /** git 分支（仅 git 仓库；空 = 默认分支） */
    private String branch;
    /** 绑定应用（项目与应用强绑定：压测/问题/接口扫描自动归属该应用） */
    private String appName;
    /** git 用户名（私有仓库，可空；列表接口不脱敏展示） */
    private String gitUser;
    /** git 密码/访问令牌（明文存储，生产环境应加密；REST 响应永不返回该字段） */
    @com.fasterxml.jackson.annotation.JsonIgnore
    private String gitToken;
    /** 状态：importing/ready/failed */
    private String status;
    private Integer fileCount;
    private Long totalSize;
    private String errorMsg;
    private LocalDateTime createTime;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getRepoName() {
        return repoName;
    }

    public void setRepoName(String repoName) {
        this.repoName = repoName;
    }

    public String getRepoType() {
        return repoType;
    }

    public void setRepoType(String repoType) {
        this.repoType = repoType;
    }

    public String getSourcePath() {
        return sourcePath;
    }

    public void setSourcePath(String sourcePath) {
        this.sourcePath = sourcePath;
    }

    public String getBranch() {
        return branch;
    }

    public void setBranch(String branch) {
        this.branch = branch;
    }

    public String getAppName() {
        return appName;
    }

    public void setAppName(String appName) {
        this.appName = appName;
    }

    public String getGitUser() {
        return gitUser;
    }

    public void setGitUser(String gitUser) {
        this.gitUser = gitUser;
    }

    public String getGitToken() {
        return gitToken;
    }

    public void setGitToken(String gitToken) {
        this.gitToken = gitToken;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public Integer getFileCount() {
        return fileCount;
    }

    public void setFileCount(Integer fileCount) {
        this.fileCount = fileCount;
    }

    public Long getTotalSize() {
        return totalSize;
    }

    public void setTotalSize(Long totalSize) {
        this.totalSize = totalSize;
    }

    public String getErrorMsg() {
        return errorMsg;
    }

    public void setErrorMsg(String errorMsg) {
        this.errorMsg = errorMsg;
    }

    public LocalDateTime getCreateTime() {
        return createTime;
    }

    public void setCreateTime(LocalDateTime createTime) {
        this.createTime = createTime;
    }
}
