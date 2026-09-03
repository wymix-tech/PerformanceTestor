package com.perftestor.datagen.model;

import java.util.List;

/**
 * 字段生成规则（规则 JSON 契约的模块内强类型模型，宿主适配器负责 JSON → 本模型转换）。
 *
 * <p>规则 JSON 契约（前端编辑器 / 模块引擎 / AI 工具三方共同遵守）：</p>
 * <pre>
 * {"order.user_id": {"type": "refSkew", "table": "user", "column": "id", "hotPercent": 20, "hotShare": 80},
 *  "order.status":  {"type": "enum", "values": ["NEW","PAID"], "weights": [10, 80]},
 *  "order.amount":  {"type": "range", "min": 100, "max": 5000,
 *                    "overThreshold": {"op": "&gt;", "value": 1000, "share": 30}},
 *   "order.order_no": {"type": "seq", "seqPrefix": "ORD_", "seqStart": 1}}
 * </pre>
 *
 * <p>seq（单调递增序号）用于唯一索引/业务编码列，保证批量生成不撞唯一键：
 * 值为 {@code seqPrefix + (seqStart + rowIndex)}。{@code seqStart} 由宿主在<b>执行时</b>
 * 探测目标列 {@code MAX(col)+1} 刷新——数据方案要支持重复执行，起始值不能固化在方案里。</p>
 */
public class FieldRule {

    public static final String TYPE_FIXED = "fixed";
    public static final String TYPE_RANGE = "range";
    public static final String TYPE_ENUM = "enum";
    public static final String TYPE_REF = "ref";
    public static final String TYPE_REF_SKEW = "refSkew";
    /** 单调递增序号：唯一索引/业务编码列防冲突 */
    public static final String TYPE_SEQ = "seq";

    /** 规则类型：fixed/range/enum/ref/refSkew（未知类型回落默认生成器） */
    private String type;

    /** fixed：固定值 */
    private Object value;

    /** range：区间 [min, max] */
    private Double min;
    private Double max;

    /** enum：候选值集合（与 overThreshold 的阈值联动见 OverThreshold） */
    private List<Object> values;

    /** enum：权重分布（与 values 等长，空则均匀分布） */
    private List<Double> weights;

    /** ref/refSkew：父表与父列 */
    private String table;
    private String column;

    /** refSkew：热点倾斜（hotPercent% 的父行承接 hotShare% 的子行） */
    private Double hotPercent;
    private Double hotShare;

    /** range：阈值越界分布（保证一定比例行越过代码谓词阈值） */
    private OverThreshold overThreshold;

    /** seq：序号前缀（可空，空则纯数字递增） */
    private String seqPrefix;
    /** seq：序号起始值（由宿主执行时刷新；缺省 1） */
    private Long seqStart;

    public static FieldRule fixed(Object value) {
        FieldRule r = new FieldRule();
        r.type = TYPE_FIXED;
        r.value = value;
        return r;
    }

    public String getType() {
        return type;
    }

    public void setType(String type) {
        this.type = type;
    }

    public Object getValue() {
        return value;
    }

    public void setValue(Object value) {
        this.value = value;
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

    public List<Object> getValues() {
        return values;
    }

    public void setValues(List<Object> values) {
        this.values = values;
    }

    public List<Double> getWeights() {
        return weights;
    }

    public void setWeights(List<Double> weights) {
        this.weights = weights;
    }

    public String getTable() {
        return table;
    }

    public void setTable(String table) {
        this.table = table;
    }

    public String getColumn() {
        return column;
    }

    public void setColumn(String column) {
        this.column = column;
    }

    public Double getHotPercent() {
        return hotPercent;
    }

    public void setHotPercent(Double hotPercent) {
        this.hotPercent = hotPercent;
    }

    public Double getHotShare() {
        return hotShare;
    }

    public void setHotShare(Double hotShare) {
        this.hotShare = hotShare;
    }

    public OverThreshold getOverThreshold() {
        return overThreshold;
    }

    public void setOverThreshold(OverThreshold overThreshold) {
        this.overThreshold = overThreshold;
    }

    public String getSeqPrefix() {
        return seqPrefix;
    }

    public void setSeqPrefix(String seqPrefix) {
        this.seqPrefix = seqPrefix;
    }

    public Long getSeqStart() {
        return seqStart;
    }

    public void setSeqStart(Long seqStart) {
        this.seqStart = seqStart;
    }

    /** 范围类阈值规则：保证 share% 的行取 (value, max] 区间（命中代码中 amount > value 类谓词） */
    public static class OverThreshold {
        /** 比较符：&gt; 或 &gt;= */
        private String op = ">";
        private double value;
        /** 越过阈值的行占比（0~100） */
        private double share = 30;

        public String getOp() {
            return op;
        }

        public void setOp(String op) {
            this.op = op;
        }

        public double getValue() {
            return value;
        }

        public void setValue(double value) {
            this.value = value;
        }

        public double getShare() {
            return share;
        }

        public void setShare(double share) {
            this.share = share;
        }
    }
}
