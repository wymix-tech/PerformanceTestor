package com.perftestor.datagen.model;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 表组元素：联表灌数的单表配置。
 * fields 的键为目标列名（大小写不敏感，引擎侧统一小写处理）。
 */
public class TableSpec {

    /** 目标表名 */
    private String tableName;

    /** 目标行数（1 ~ maxRows） */
    private int rowCount = 1000;

    /** 字段生成规则（列名 → 规则），未配置的列走类型默认生成器 */
    private Map<String, FieldRule> fields = new LinkedHashMap<>();

    /** 表级孤儿率覆盖（空则用 DataGenSpec.orphanRate） */
    private Double orphanRate;

    public TableSpec() {
    }

    public TableSpec(String tableName, int rowCount) {
        this.tableName = tableName;
        this.rowCount = rowCount;
    }

    public String getTableName() {
        return tableName;
    }

    public void setTableName(String tableName) {
        this.tableName = tableName;
    }

    public int getRowCount() {
        return rowCount;
    }

    public void setRowCount(int rowCount) {
        this.rowCount = rowCount;
    }

    public Map<String, FieldRule> getFields() {
        return fields;
    }

    public void setFields(Map<String, FieldRule> fields) {
        this.fields = fields == null ? new LinkedHashMap<>() : fields;
    }

    public Double getOrphanRate() {
        return orphanRate;
    }

    public void setOrphanRate(Double orphanRate) {
        this.orphanRate = orphanRate;
    }
}
