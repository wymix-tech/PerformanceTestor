package com.perfserver.datagen;

/**
 * 目标表列元信息：来自 DatabaseMetaData.getColumns。
 */
public class ColumnInfo {

    private String name;
    /** JDBC 类型名（如 VARCHAR / INT / DATETIME） */
    private String typeName;
    /** 列大小（字符或字节） */
    private int size;
    /** 是否允许 NULL */
    private boolean nullable;
    /** 是否自增（自增列不参与插入） */
    private boolean autoIncrement;
    /** 是否主键列 */
    private boolean primaryKey;
    /** 列注释（枚举值发现来源之一） */
    private String comment;
    /** 枚举候选值建议（ENUM/CHECK/注释/源码扫描四级发现） */
    private java.util.List<Object> suggestedValues;
    /** 疑似枚举列（值发现失败但列名含 status/type/state/flag 等特征） */
    private boolean suspectEnum;

    public ColumnInfo() {
    }

    public ColumnInfo(String name, String typeName, int size, boolean nullable,
                      boolean autoIncrement, boolean primaryKey) {
        this.name = name;
        this.typeName = typeName;
        this.size = size;
        this.nullable = nullable;
        this.autoIncrement = autoIncrement;
        this.primaryKey = primaryKey;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public String getTypeName() {
        return typeName;
    }

    public void setTypeName(String typeName) {
        this.typeName = typeName;
    }

    public int getSize() {
        return size;
    }

    public void setSize(int size) {
        this.size = size;
    }

    public boolean isNullable() {
        return nullable;
    }

    public void setNullable(boolean nullable) {
        this.nullable = nullable;
    }

    public boolean isAutoIncrement() {
        return autoIncrement;
    }

    public void setAutoIncrement(boolean autoIncrement) {
        this.autoIncrement = autoIncrement;
    }

    public boolean isPrimaryKey() {
        return primaryKey;
    }

    public void setPrimaryKey(boolean primaryKey) {
        this.primaryKey = primaryKey;
    }

    public String getComment() {
        return comment;
    }

    public void setComment(String comment) {
        this.comment = comment;
    }

    public java.util.List<Object> getSuggestedValues() {
        return suggestedValues;
    }

    public void setSuggestedValues(java.util.List<Object> suggestedValues) {
        this.suggestedValues = suggestedValues;
    }

    public boolean isSuspectEnum() {
        return suspectEnum;
    }

    public void setSuspectEnum(boolean suspectEnum) {
        this.suspectEnum = suspectEnum;
    }
}
