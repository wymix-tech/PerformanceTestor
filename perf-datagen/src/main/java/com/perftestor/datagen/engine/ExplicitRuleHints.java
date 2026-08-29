package com.perftestor.datagen.engine;

import com.perftestor.datagen.model.DataGenSpec;
import com.perftestor.datagen.model.FieldRule;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SchemaMetadata;
import com.perftestor.datagen.model.TableSpec;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 显式规则提取：从 TableSpec.fields 中提取 ref/refSkew 边（四级来源中最高优先级）。
 * 指向组外表的规则被忽略（引擎层 dry-run 产出警告）。
 */
final class ExplicitRuleHints {

    private ExplicitRuleHints() {
    }

    static java.util.List<RelationHint> extract(DataGenSpec spec, Map<String, SchemaMetadata> metaMap) {
        List<RelationHint> hints = new ArrayList<>();
        if (spec == null) {
            return hints;
        }
        for (TableSpec ts : spec.getTables()) {
            String childTable = ts.getTableName() == null ? null : ts.getTableName().toLowerCase(Locale.ROOT);
            if (!metaMap.containsKey(childTable)) {
                continue;
            }
            for (Map.Entry<String, FieldRule> e : ts.getFields().entrySet()) {
                FieldRule fr = e.getValue();
                if (fr == null) {
                    continue;
                }
                boolean isRef = FieldRule.TYPE_REF.equals(fr.getType())
                        || FieldRule.TYPE_REF_SKEW.equals(fr.getType());
                if (!isRef || fr.getTable() == null || fr.getColumn() == null) {
                    continue;
                }
                String parentTable = fr.getTable().toLowerCase(Locale.ROOT);
                if (!metaMap.containsKey(parentTable)) {
                    continue;
                }
                RelationHint h = new RelationHint(childTable, e.getKey(),
                        parentTable, fr.getColumn(), RelationHint.SRC_EXPLICIT, 1.0);
                h.getSourceRefs().add("request");
                hints.add(h);
            }
        }
        return hints;
    }
}
