package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 灌数据任务记录：对应 data_gen_task 表。
 */
public class DataGenTask {

    private Long id;
    private String taskName;
    private String dbUrl;
    private String tableName;
    private Integer rowCount;
    private String ruleJson;
    /** 状态：pending/running/finished/failed */
    private String status;
    private Integer inserted;
    /** 总目标行数（表组求和） */
    private Integer totalRows;
    /** 表组请求 JSON（联表形态） */
    private String tablesJson;
    /** 执行详情 JSON（逐表统计/警告/试运行报告/自检报告） */
    private String detailJson;
    private Long costMs;
    private String errorMsg;
    private LocalDateTime createTime;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getTaskName() {
        return taskName;
    }

    public void setTaskName(String taskName) {
        this.taskName = taskName;
    }

    public String getDbUrl() {
        return dbUrl;
    }

    public void setDbUrl(String dbUrl) {
        this.dbUrl = dbUrl;
    }

    public String getTableName() {
        return tableName;
    }

    public void setTableName(String tableName) {
        this.tableName = tableName;
    }

    public Integer getRowCount() {
        return rowCount;
    }

    public void setRowCount(Integer rowCount) {
        this.rowCount = rowCount;
    }

    public String getRuleJson() {
        return ruleJson;
    }

    public void setRuleJson(String ruleJson) {
        this.ruleJson = ruleJson;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public Integer getInserted() {
        return inserted;
    }

    public void setInserted(Integer inserted) {
        this.inserted = inserted;
    }

    public Integer getTotalRows() {
        return totalRows;
    }

    public void setTotalRows(Integer totalRows) {
        this.totalRows = totalRows;
    }

    public String getTablesJson() {
        return tablesJson;
    }

    public void setTablesJson(String tablesJson) {
        this.tablesJson = tablesJson;
    }

    public String getDetailJson() {
        return detailJson;
    }

    public void setDetailJson(String detailJson) {
        this.detailJson = detailJson;
    }

    public Long getCostMs() {
        return costMs;
    }

    public void setCostMs(Long costMs) {
        this.costMs = costMs;
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
