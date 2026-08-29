package com.perftestor.datagen.model;

import java.util.ArrayList;
import java.util.List;

/**
 * 魔法值提示：源码扫描产出的「表列 → 候选值」建议（含来源定位与范围谓词阈值）。
 * 宿主落 semantic_hint 库，经建议接口返回前端一键采纳为生成规则。
 */
public class MagicHint {

    public static final String KIND_LITERAL = "literal";   // SQL 字面量（WHERE status = 'PAID'）
    public static final String KIND_CONSTANT = "constant"; // static final 常量
    public static final String KIND_ENUM = "enum";         // Java 枚举常量
    public static final String KIND_RANGE = "range";       // 比较谓词（amount > 1000）

    private String table;
    private String column;
    /** 候选值集合（枚举/字面量/常量；数值列存 Number） */
    private List<Object> values = new ArrayList<>();
    /** 范围谓词阈值（kind=range 时非空） */
    private FieldRule.OverThreshold threshold;
    private String kind;
    /** 来源定位列表（"src/main/.../OrderMapper.java:42"） */
    private List<String> sourceRefs = new ArrayList<>();

    public MagicHint() {
    }

    public MagicHint(String table, String column, String kind) {
        this.table = lower(table);
        this.column = lower(column);
        this.kind = kind;
    }

    private static String lower(String s) {
        return s == null ? null : s.toLowerCase(java.util.Locale.ROOT);
    }

    public String getTable() {
        return table;
    }

    public String getColumn() {
        return column;
    }

    public List<Object> getValues() {
        return values;
    }

    public FieldRule.OverThreshold getThreshold() {
        return threshold;
    }

    public void setThreshold(FieldRule.OverThreshold threshold) {
        this.threshold = threshold;
    }

    public String getKind() {
        return kind;
    }

    public List<String> getSourceRefs() {
        return sourceRefs;
    }
}
