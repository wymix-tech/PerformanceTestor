package com.perftestor.datagen.model;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * 统一 schema 元数据：由 DatabaseDialect.fetchMetadata 产出。
 * 表名/列名内部统一小写（规避 MySQL 大小写不敏感与 PG/Oracle 大小写敏感差异），
 * 生成 SQL 时经 Dialect.quoteIdentifier 还原。
 */
public class SchemaMetadata {

    /** 表名（小写） */
    private String tableName;
    private List<ColumnMeta> columns = new ArrayList<>();
    private Set<String> primaryKeys = new HashSet<>();
    private List<FkMeta> foreignKeys = new ArrayList<>();
    /** 当前行数（空表兜底与抽样判断依据；方言无法快速获取时 -1） */
    private long rowCount = -1;

    /** 列名查找（大小写不敏感） */
    public ColumnMeta findColumn(String name) {
        if (name == null) {
            return null;
        }
        for (ColumnMeta c : columns) {
            if (c.getName().equalsIgnoreCase(name)) {
                return c;
            }
        }
        return null;
    }

    public boolean isNullOrEmptyTable() {
        return rowCount <= 0;
    }

    public String getTableName() {
        return tableName;
    }

    public void setTableName(String tableName) {
        this.tableName = tableName == null ? null : tableName.toLowerCase(Locale.ROOT);
    }

    public List<ColumnMeta> getColumns() {
        return columns;
    }

    public Set<String> getPrimaryKeys() {
        return primaryKeys;
    }

    public List<FkMeta> getForeignKeys() {
        return foreignKeys;
    }

    public long getRowCount() {
        return rowCount;
    }

    public void setRowCount(long rowCount) {
        this.rowCount = rowCount;
    }

    /** 列元信息 */
    public static class ColumnMeta {
        private String name;
        /** 方言类型名（如 VARCHAR/INT/ENUM('A','B')/text） */
        private String typeName;
        private int size;
        private boolean nullable;
        private boolean autoIncrement;
        private boolean primaryKey;
        /** 列注释（枚举值发现第四级来源「1=待支付 2=已支付」的载体） */
        private String comment;

        public String getName() {
            return name;
        }

        public void setName(String name) {
            this.name = name == null ? null : name.toLowerCase(Locale.ROOT);
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
    }

    /** 外键（声明式约束；无外键库由 RelationResolver 语义推断补齐） */
    public static class FkMeta {
        /** 子表外键列 */
        private String fkColumn;
        /** 父表名（小写） */
        private String pkTable;
        /** 父表主键列 */
        private String pkColumn;

        public FkMeta() {
        }

        public FkMeta(String fkColumn, String pkTable, String pkColumn) {
            this.fkColumn = fkColumn == null ? null : fkColumn.toLowerCase(Locale.ROOT);
            this.pkTable = pkTable == null ? null : pkTable.toLowerCase(Locale.ROOT);
            this.pkColumn = pkColumn == null ? null : pkColumn.toLowerCase(Locale.ROOT);
        }

        public String getFkColumn() {
            return fkColumn;
        }

        public String getPkTable() {
            return pkTable;
        }

        public String getPkColumn() {
            return pkColumn;
        }
    }

    /** 构建辅助：按列名索引（内部使用） */
    public Map<String, ColumnMeta> columnIndex() {
        Map<String, ColumnMeta> idx = new LinkedHashMap<>();
        for (ColumnMeta c : columns) {
            idx.put(c.getName(), c);
        }
        return idx;
    }
}
