package com.perftestor.datagen.model;

import java.util.ArrayList;
import java.util.List;

/**
 * 接口访问事实：单个压测接口在一张表上的访问结论（供宿主编排层推导行数与生成规则）。
 *
 * <p>每条事实都带 {@code sourceRefs} 证据（{@code 文件:行号}）与置信度；
 * 未解析出结论时 {@code unresolvedReason} 非空，由前端黄色提示引导手工补表，
 * 不做静默猜测。</p>
 */
public class EndpointTableFact {

    /** SQL 访问类型：读（SELECT）/ 写（INSERT/UPDATE/DELETE） */
    public enum SqlAccess {
        READ, WRITE
    }

    /** 谓词算子 */
    public enum Op {
        /** 等值：col = v */
        EQ,
        /** 集合：col IN (v1, v2) */
        IN,
        /** 范围：col &gt; v / col &lt; v / col BETWEEN a AND b */
        RANGE
    }

    /** SQL 谓词：列 + 算子 + 取值（EQ/IN 用 values；RANGE 用 min/max） */
    public static class Predicate {
        private String column;
        private Op op;
        private List<Object> values = new ArrayList<>();
        private Double min;
        private Double max;

        public Predicate() {
        }

        public Predicate(String column, Op op) {
            this.column = column;
            this.op = op;
        }

        public String getColumn() {
            return column;
        }

        public void setColumn(String column) {
            this.column = column;
        }

        public Op getOp() {
            return op;
        }

        public void setOp(Op op) {
            this.op = op;
        }

        public List<Object> getValues() {
            return values;
        }

        public Double getMin() {
            return min;
        }

        public void setMin(Double min) {
            this.min = min;
        }

        public Double getMax() {
            return max;
        }

        public void setMax(Double max) {
            this.max = max;
        }

        /** 谓词涉及的全部取值（RANGE 为空列表） */
        public List<Object> flatValues() {
            return values;
        }

        @Override
        public String toString() {
            return column + " " + op + (op == Op.RANGE ? " [" + min + "," + max + "]" : " " + values);
        }
    }

    /** 外键引用：本表外键列 → 父表列 */
    public static final class JoinRef {
        private final String childColumn;
        private final String parentTable;
        private final String parentColumn;
        private final double confidence;

        public JoinRef(String childColumn, String parentTable, String parentColumn, double confidence) {
            this.childColumn = childColumn;
            this.parentTable = parentTable;
            this.parentColumn = parentColumn;
            this.confidence = confidence;
        }

        public String getChildColumn() {
            return childColumn;
        }

        public String getParentTable() {
            return parentTable;
        }

        public String getParentColumn() {
            return parentColumn;
        }

        public double getConfidence() {
            return confidence;
        }

        @Override
        public String toString() {
            return childColumn + " → " + parentTable + "." + parentColumn;
        }
    }

    /** 小写表名 */
    private String table;
    private SqlAccess access = SqlAccess.READ;
    private List<Predicate> predicates = new ArrayList<>();
    /** JOIN 涉及的关联表（小写） */
    private List<String> joinTables = new ArrayList<>();
    /** 外键引用边：本表列 → 父表列（驱动 ref/refSkew 规则与父表补齐） */
    private List<JoinRef> joinRefs = new ArrayList<>();
    /** 跨 SQL 累积的被引用列名（N+1 形态下外键列可能只在单条 SQL 里出现） */
    private final List<String> columns = new ArrayList<>();
    /** 证据：文件:行号 */
    private List<String> sourceRefs = new ArrayList<>();
    /** 置信度 0~1：多实现/降级记 0.4~0.6 */
    private double confidence = 1.0;
    /** 非空表示降级原因（未覆盖/子查询/多表 UPDATE/超时等） */
    private String unresolvedReason;
    /** 分页大小（SQL limit 字面量，0 表示未识别） */
    private int pageSize;
    /** 是否为详情类访问（按 id 查单行，数据应做热点倾斜） */
    private boolean detailLookup;

    public EndpointTableFact() {
    }

    public EndpointTableFact(String table, SqlAccess access) {
        this.table = table == null ? null : table.toLowerCase(java.util.Locale.ROOT);
        this.access = access == null ? SqlAccess.READ : access;
    }

    public String getTable() {
        return table;
    }

    public void setTable(String table) {
        this.table = table;
    }

    public SqlAccess getAccess() {
        return access;
    }

    public void setAccess(SqlAccess access) {
        this.access = access;
    }

    public List<Predicate> getPredicates() {
        return predicates;
    }

    public List<String> getJoinTables() {
        return joinTables;
    }

    public List<JoinRef> getJoinRefs() {
        return joinRefs;
    }

    /** 累积列名（小写，去重由调用方保证） */
    public List<String> getColumns() {
        return columns;
    }

    public List<String> getSourceRefs() {
        return sourceRefs;
    }

    public double getConfidence() {
        return confidence;
    }

    public void setConfidence(double confidence) {
        this.confidence = confidence;
    }

    public String getUnresolvedReason() {
        return unresolvedReason;
    }

    public void setUnresolvedReason(String unresolvedReason) {
        this.unresolvedReason = unresolvedReason;
    }

    public int getPageSize() {
        return pageSize;
    }

    public void setPageSize(int pageSize) {
        this.pageSize = pageSize;
    }

    public boolean isDetailLookup() {
        return detailLookup;
    }

    public void setDetailLookup(boolean detailLookup) {
        this.detailLookup = detailLookup;
    }

    @Override
    public String toString() {
        return table + "/" + access + " predicates=" + predicates + " conf=" + confidence;
    }
}
