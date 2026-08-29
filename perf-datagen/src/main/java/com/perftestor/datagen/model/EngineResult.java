package com.perftestor.datagen.model;

import java.util.ArrayList;
import java.util.List;

/**
 * 引擎执行结果：成功/失败、行数统计、试运行估算、警告清单（含逐表统计）。
 */
public class EngineResult {

    private boolean success;
    private boolean dryRun;
    private long totalRows;
    private long insertedRows;
    /** 试运行估算的最大单表事务时长参考（ms，0 表示无估算） */
    private long estimatedMaxTxnMs;
    private List<String> warnings = new ArrayList<>();
    private List<TableStat> tableStats = new ArrayList<>();
    private String errorMsg;

    public static EngineResult failure(String msg) {
        EngineResult r = new EngineResult();
        r.success = false;
        r.errorMsg = msg;
        return r;
    }

    public boolean isSuccess() {
        return success;
    }

    public void setSuccess(boolean success) {
        this.success = success;
    }

    public boolean isDryRun() {
        return dryRun;
    }

    public void setDryRun(boolean dryRun) {
        this.dryRun = dryRun;
    }

    public long getTotalRows() {
        return totalRows;
    }

    public void setTotalRows(long totalRows) {
        this.totalRows = totalRows;
    }

    public long getInsertedRows() {
        return insertedRows;
    }

    public void setInsertedRows(long insertedRows) {
        this.insertedRows = insertedRows;
    }

    public long getEstimatedMaxTxnMs() {
        return estimatedMaxTxnMs;
    }

    public void setEstimatedMaxTxnMs(long estimatedMaxTxnMs) {
        this.estimatedMaxTxnMs = estimatedMaxTxnMs;
    }

    public List<String> getWarnings() {
        return warnings;
    }

    public List<TableStat> getTableStats() {
        return tableStats;
    }

    public String getErrorMsg() {
        return errorMsg;
    }

    public void setErrorMsg(String errorMsg) {
        this.errorMsg = errorMsg;
    }

    /** 逐表统计 */
    public static class TableStat {
        private String table;
        private long rows;

        public TableStat() {
        }

        public TableStat(String table, long rows) {
            this.table = table;
            this.rows = rows;
        }

        public String getTable() {
            return table;
        }

        public long getRows() {
            return rows;
        }
    }
}
