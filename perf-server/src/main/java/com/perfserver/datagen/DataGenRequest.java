package com.perfserver.datagen;

/**
 * 数据灌入请求：连接被测库，按表结构批量生成测试数据。
 */
public class DataGenRequest {

    /** 任务名称（可选） */
    private String taskName;
    /** 连接配置 ID（可选，与"问题与优化"页的数据库连接配置共用；提供时覆盖下方手填连接） */
    private Long connId;
    /** 目标库 JDBC URL，如 jdbc:mysql://127.0.0.1:3306/demo 或 jdbc:postgresql://127.0.0.1:5432/demo */
    private String dbUrl;
    private String dbUser;
    private String dbPassword;
    /** 目标表名 */
    private String tableName;
    /** 目标行数（上限 100 万） */
    private int rowCount = 1000;
    /**
     * 字段生成规则 JSON（单表形态），如：
     * {"fields":{"age":{"type":"range","min":18,"max":60},"status":{"type":"enum","values":[0,1,2]}}}
     */
    private String ruleJson;
    /** 表组（联表形态；空则退化为单表 tableName/rowCount） */
    private java.util.List<TableSpecDto> tables;
    /** 全局孤儿率（0~1，默认 0） */
    private Double orphanRate;
    /** 试运行：完整校验预检但不 INSERT（结果写入任务详情） */
    private boolean dryRun;
    /** 源码仓库 ID（可选；提供时扫描 MyBatis 源码做魔法值/关联语义识别） */
    private Long repoId;

    /** 表组元素 DTO（fields 为字段规则 JSON，形态同 ruleJson.fields 值） */
    public static class TableSpecDto {
        private String tableName;
        private int rowCount = 1000;
        /** 字段规则 JSON：{"col":{"type":"refSkew",...},...} */
        private String fields;
        private Double orphanRate;

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

        public String getFields() {
            return fields;
        }

        public void setFields(String fields) {
            this.fields = fields;
        }

        public Double getOrphanRate() {
            return orphanRate;
        }

        public void setOrphanRate(Double orphanRate) {
            this.orphanRate = orphanRate;
        }
    }

    public java.util.List<TableSpecDto> getTables() {
        return tables;
    }

    public void setTables(java.util.List<TableSpecDto> tables) {
        this.tables = tables;
    }

    public Double getOrphanRate() {
        return orphanRate;
    }

    public void setOrphanRate(Double orphanRate) {
        this.orphanRate = orphanRate;
    }

    public boolean isDryRun() {
        return dryRun;
    }

    public void setDryRun(boolean dryRun) {
        this.dryRun = dryRun;
    }

    public Long getRepoId() {
        return repoId;
    }

    public void setRepoId(Long repoId) {
        this.repoId = repoId;
    }

    public String getTaskName() {
        return taskName;
    }

    public void setTaskName(String taskName) {
        this.taskName = taskName;
    }

    public Long getConnId() {
        return connId;
    }

    public void setConnId(Long connId) {
        this.connId = connId;
    }

    public String getDbUrl() {
        return dbUrl;
    }

    public void setDbUrl(String dbUrl) {
        this.dbUrl = dbUrl;
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

    public String getRuleJson() {
        return ruleJson;
    }

    public void setRuleJson(String ruleJson) {
        this.ruleJson = ruleJson;
    }
}
