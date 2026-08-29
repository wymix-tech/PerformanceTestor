package com.perftestor.datagen.engine;

import com.perftestor.datagen.model.DataGenSpec;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SchemaMetadata;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.sql.Connection;

/**
 * 关联关系解析：四级来源合并（explicit &gt; db_fk &gt; orm_join &gt; naming）。
 * naming 系不自动生效，经 pendingSuggestions 交宿主确认采纳。
 */
public class RelationResolver {

    private final NamingRelationResolver namingResolver = new NamingRelationResolver();
    /** ORM 解析器注入的关联语义（orm_join 来源） */
    private volatile List<RelationHint> ormHints = List.of();

    public void registerSource(List<RelationHint> hints) {
        this.ormHints = hints == null ? List.of() : hints;
    }

    /** 解析表组关联，产出自动生效边集与待确认建议 */
    public RelationResolution resolve(Collection<SchemaMetadata> metas, DataGenSpec spec,
                                      Connection conn, com.perftestor.datagen.spi.DatabaseDialect dialect) {
        Map<String, SchemaMetadata> metaMap = new LinkedHashMap<>();
        for (SchemaMetadata m : metas) {
            metaMap.put(m.getTableName(), m);
        }
        Map<String, RelationHint> best = new LinkedHashMap<>();

        // 来源 2：db_fk 声明外键（自动应用）
        for (SchemaMetadata m : metas) {
            for (SchemaMetadata.FkMeta fk : m.getForeignKeys()) {
                RelationHint h = new RelationHint(m.getTableName(), fk.getFkColumn(),
                        fk.getPkTable(), fk.getPkColumn(), RelationHint.SRC_DB_FK, 1.0);
                h.getSourceRefs().add("metadata");
                best.merge(edgeKey(h), h, (a, b) -> better(a, b));
            }
        }

        // 来源 3：orm_join 源码语义（自动应用）
        for (RelationHint h : ormHints) {
            best.merge(edgeKey(h), h, (a, b) -> better(a, b));
        }

        // 来源 1：explicit 显式规则（最高优先级）
        for (RelationHint h : ExplicitRuleHints.extract(spec, metaMap)) {
            best.merge(edgeKey(h), h, (a, b) -> better(a, b));
        }

        // 来源 4：naming 命名推断（父表空表时 naming_empty_fallback；低置信交确认）
        List<RelationHint> pending = new ArrayList<>();
        for (SchemaMetadata m : metas) {
            Map<String, SchemaMetadata> parents = new LinkedHashMap<>();
            for (SchemaMetadata other : metas) {
                if (!other.getTableName().equals(m.getTableName())) {
                    parents.put(other.getTableName(), other);
                }
            }
            for (RelationHint h : namingResolver.resolve(m, parents, conn, dialect)) {
                if (best.containsKey(edgeKey(h))) {
                    continue;
                }
                if (h.autoApplicable()) {
                    best.put(edgeKey(h), h);
                } else {
                    pending.add(h);
                }
            }
        }

        RelationResolution r = new RelationResolution();
        r.autoEdges = new ArrayList<>(best.values());
        r.pendingSuggestions = pending;
        return r;
    }

    static String edgeKey(RelationHint h) {
        return h.getChildTable() + "." + h.getChildColumn();
    }

    static RelationHint better(RelationHint a, RelationHint b) {
        return a.priority() >= b.priority() ? a : b;
    }

    /** 解析产物：自动生效边集 + 待确认建议 */
    public static class RelationResolution {
        private List<RelationHint> autoEdges = new ArrayList<>();
        private List<RelationHint> pendingSuggestions = new ArrayList<>();

        public List<RelationHint> getAutoEdges() {
            return autoEdges;
        }

        public List<RelationHint> getPendingSuggestions() {
            return pendingSuggestions;
        }
    }
}

