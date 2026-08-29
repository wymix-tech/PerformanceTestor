package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 语义提示：魔法值候选（value）与表间关联建议（relation），对应 semantic_hint 表。
 * 灌数引擎回调产出，供前端建议面板确认采纳。
 */
public class SemanticHint {

    public static final String TYPE_VALUE = "value";
    public static final String TYPE_RELATION = "relation";
    public static final String ST_PENDING = "pending";
    public static final String ST_ADOPTED = "adopted";
    public static final String ST_DISMISSED = "dismissed";

    private Long id;
    private Long repoId;
    private String hintType;
    private String tableName;
    private String columnName;
    private String valuesJson;
    private String parentTable;
    private String parentColumn;
    private String sourceRefs;
    private String source;
    private Double confidence;
    private String status;
    private LocalDateTime createTime;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public Long getRepoId() { return repoId; }
    public void setRepoId(Long repoId) { this.repoId = repoId; }
    public String getHintType() { return hintType; }
    public void setHintType(String hintType) { this.hintType = hintType; }
    public String getTableName() { return tableName; }
    public void setTableName(String tableName) { this.tableName = tableName; }
    public String getColumnName() { return columnName; }
    public void setColumnName(String columnName) { this.columnName = columnName; }
    public String getValuesJson() { return valuesJson; }
    public void setValuesJson(String valuesJson) { this.valuesJson = valuesJson; }
    public String getParentTable() { return parentTable; }
    public void setParentTable(String parentTable) { this.parentTable = parentTable; }
    public String getParentColumn() { return parentColumn; }
    public void setParentColumn(String parentColumn) { this.parentColumn = parentColumn; }
    public String getSourceRefs() { return sourceRefs; }
    public void setSourceRefs(String sourceRefs) { this.sourceRefs = sourceRefs; }
    public String getSource() { return source; }
    public void setSource(String source) { this.source = source; }
    public Double getConfidence() { return confidence; }
    public void setConfidence(Double confidence) { this.confidence = confidence; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
    public LocalDateTime getCreateTime() { return createTime; }
    public void setCreateTime(LocalDateTime createTime) { this.createTime = createTime; }
}

