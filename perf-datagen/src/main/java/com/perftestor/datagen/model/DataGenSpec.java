package com.perftestor.datagen.model;

import java.util.ArrayList;
import java.util.List;

/**
 * 灌数规格：宿主 → 引擎的唯一入参模型。
 * 兼容旧单表形态（tables 仅一个元素）与多表组形态；sourceFiles 为可选源码输入
 * （磁盘目录遍历结果或宿主源码索引适配产物），为空时引擎降级为纯数据库元数据模式。
 */
public class DataGenSpec {

    private String jdbcUrl;
    private String dbUser;
    private String dbPassword;

    /** 表组（旧单表请求等价于单元素表组） */
    private List<TableSpec> tables = new ArrayList<>();

    /** 全局孤儿率（0~1，表级 orphanRate 可覆盖；默认 0） */
    private Double orphanRate;

    /** 行数上限保护（表组求和不得超过） */
    private int maxRows = 1_000_000;

    /** 试运行：完整校验预检但不 INSERT */
    private boolean dryRun;

    /** 可选源码文件（.java/.xml，MyBatis 解析与 Java 枚举反查输入） */
    private List<SourceFile> sourceFiles;

    public String getJdbcUrl() {
        return jdbcUrl;
    }

    public void setJdbcUrl(String jdbcUrl) {
        this.jdbcUrl = jdbcUrl;
    }

    public String getDbUser() {
        return dbUser;
    }

    public void setDbUser(String dbUser) {
        this.dbUser = dbUser;
    }

    public String getDbPassword() {
        return dbPassword;
    }

    public void setDbPassword(String dbPassword) {
        this.dbPassword = dbPassword;
    }

    public List<TableSpec> getTables() {
        return tables;
    }

    public void setTables(List<TableSpec> tables) {
        this.tables = tables == null ? new ArrayList<>() : tables;
    }

    public Double getOrphanRate() {
        return orphanRate;
    }

    public void setOrphanRate(Double orphanRate) {
        this.orphanRate = orphanRate;
    }

    public int getMaxRows() {
        return maxRows;
    }

    public void setMaxRows(int maxRows) {
        this.maxRows = maxRows;
    }

    public boolean isDryRun() {
        return dryRun;
    }

    public void setDryRun(boolean dryRun) {
        this.dryRun = dryRun;
    }

    public List<SourceFile> getSourceFiles() {
        return sourceFiles;
    }

    public void setSourceFiles(List<SourceFile> sourceFiles) {
        this.sourceFiles = sourceFiles;
    }
}
