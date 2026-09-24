package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 压测模板：把常用压测场景的参数沉淀下来，避免每次重填（stress_template 表）。
 *
 * <p>{@code taskJson} 存 {@code StressTaskRequest} 的参数子集（URL/方法/并发/时长/预热/
 * QPS/分档/请求头/断言/场景步骤等），由 {@link com.perfserver.service.StressTemplateService}
 * 校验与归一化，前端「从模板填充」时反序列化回表单。
 */
public class StressTemplate {

    private Long id;
    private String templateName;
    /** 归属应用（空 = 通用模板，任何项目可见） */
    private String appName;
    /** 压测请求参数 JSON */
    private String taskJson;
    private String description;
    /** 创建人（JWT subject / ci / unknown） */
    private String creator;
    private LocalDateTime createTime;
    private LocalDateTime updateTime;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getTemplateName() {
        return templateName;
    }

    public void setTemplateName(String templateName) {
        this.templateName = templateName;
    }

    public String getAppName() {
        return appName;
    }

    public void setAppName(String appName) {
        this.appName = appName;
    }

    public String getTaskJson() {
        return taskJson;
    }

    public void setTaskJson(String taskJson) {
        this.taskJson = taskJson;
    }

    public String getDescription() {
        return description;
    }

    public void setDescription(String description) {
        this.description = description;
    }

    public String getCreator() {
        return creator;
    }

    public void setCreator(String creator) {
        this.creator = creator;
    }

    public LocalDateTime getCreateTime() {
        return createTime;
    }

    public void setCreateTime(LocalDateTime createTime) {
        this.createTime = createTime;
    }

    public LocalDateTime getUpdateTime() {
        return updateTime;
    }

    public void setUpdateTime(LocalDateTime updateTime) {
        this.updateTime = updateTime;
    }
}
