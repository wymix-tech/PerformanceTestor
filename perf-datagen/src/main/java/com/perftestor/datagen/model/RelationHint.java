package com.perftestor.datagen.model;

import java.util.ArrayList;
import java.util.List;

/**
 * 表间关联提示：RelationResolver 四级来源的统一载体。
 *
 * <p>来源优先级（高 → 低）：explicit &gt; db_fk &gt; orm_join &gt; naming / naming_empty_fallback。
 * 同一条边多来源时取最高优先级；naming 类低置信推断不静默生效，经宿主建议接口确认后采纳。</p>
 */
public class RelationHint {

    public static final String SRC_EXPLICIT = "explicit";
    public static final String SRC_DB_FK = "db_fk";
    public static final String SRC_ORM_JOIN = "orm_join";
    public static final String SRC_NAMING = "naming";
    /** 空表兜底：父表 0 行时无条件采纳命名推断，但置信度 LOW，提示用户验收时确认 */
    public static final String SRC_NAMING_EMPTY = "naming_empty_fallback";

    private String childTable;
    private String childColumn;
    private String parentTable;
    private String parentColumn;
    /** 来源：explicit/db_fk/orm_join/naming/naming_empty_fallback */
    private String source;
    /** 置信度 0~1（db_fk=1.0、orm_join=0.9、naming 抽样验证 0.7+、空表兜底 0.3） */
    private double confidence;
    /** 来源定位（文件:行号 或 "metadata"） */
    private List<String> sourceRefs = new ArrayList<>();

    public RelationHint() {
    }

    public RelationHint(String childTable, String childColumn, String parentTable, String parentColumn,
                        String source, double confidence) {
        this.childTable = lower(childTable);
        this.childColumn = lower(childColumn);
        this.parentTable = lower(parentTable);
        this.parentColumn = lower(parentColumn);
        this.source = source;
        this.confidence = confidence;
    }

    /** 空表兜底工厂：命名推断无条件采纳但低置信 */
    public static RelationHint namingFallback(String childTable, String childColumn,
                                              String parentTable, String parentColumn) {
        RelationHint h = new RelationHint(childTable, childColumn, parentTable, parentColumn,
                SRC_NAMING_EMPTY, 0.3);
        h.sourceRefs.add("naming-convention(empty-table-fallback)");
        return h;
    }

    /** 来源优先级数值（越大越优先） */
    public int priority() {
        switch (source == null ? "" : source) {
            case SRC_EXPLICIT:
                return 40;
            case SRC_DB_FK:
                return 30;
            case SRC_ORM_JOIN:
                return 20;
            case SRC_NAMING:
            case SRC_NAMING_EMPTY:
                return 10;
            default:
                return 0;
        }
    }

    /** 是否可自动应用（db_fk / orm_join 高置信自动生效；naming 系需用户确认） */
    public boolean autoApplicable() {
        return SRC_EXPLICIT.equals(source) || SRC_DB_FK.equals(source) || SRC_ORM_JOIN.equals(source);
    }

    private static String lower(String s) {
        return s == null ? null : s.toLowerCase(java.util.Locale.ROOT);
    }

    public String getChildTable() {
        return childTable;
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

    public String getSource() {
        return source;
    }

    public double getConfidence() {
        return confidence;
    }

    public List<String> getSourceRefs() {
        return sourceRefs;
    }
}
