package com.perfserver.datagen;

/**
 * 数据灌入请求：连接被测库，按表结构批量生成测试数据。
 */
public class DataGenRequest {

    /** 任务名称（可选） */
    private String taskName;
    /** 目标库 JDBC URL，如 jdbc:mysql://127.0.0.1:3306/demo */
    private String dbUrl;
    private String dbUser;
    private String dbPassword;
    /** 目标表名 */
    private String tableName;
    /** 目标行数（上限 100 万） */
    private int rowCount = 1000;
    /**
     * 字段生成规则 JSON，如：
     * {"fields":{"age":{"type":"range","min":18,"max":60},"status":{"type":"enum","values":[0,1,2]}}}
     */
    private String ruleJson;

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
