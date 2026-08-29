package com.perftestor.datagen;

import com.perftestor.datagen.engine.NamingRelationResolver;
import com.perftestor.datagen.engine.RuleEngine;
import com.perftestor.datagen.enumv.EnumValueDiscoverer;
import com.perftestor.datagen.model.FieldRule;
import com.perftestor.datagen.model.MagicHint;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SchemaMetadata;
import com.perftestor.datagen.util.SqlLiteralExtractor;
import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 语义层单测：规则分布 / 枚举发现 / 命名推断与空表兜底 / SQL 抽取。 */
class SemanticParserTest {

    private static SchemaMetadata.ColumnMeta col(String name, String type) {
        SchemaMetadata.ColumnMeta c = new SchemaMetadata.ColumnMeta();
        c.setName(name);
        c.setTypeName(type);
        c.setSize(64);
        c.setNullable(true);
        return c;
    }

    private static SchemaMetadata table(String name) {
        SchemaMetadata m = new SchemaMetadata();
        m.setTableName(name);
        return m;
    }

    @Test
    void rangeOverThresholdShare() {
        FieldRule rule = new FieldRule();
        rule.setType(FieldRule.TYPE_RANGE);
        rule.setMin(100.0);
        rule.setMax(5000.0);
        FieldRule.OverThreshold ot = new FieldRule.OverThreshold();
        ot.setValue(1000);
        ot.setShare(30);
        rule.setOverThreshold(ot);
        int over = 0, total = 20_000;
        for (int i = 0; i < total; i++) {
            if (((Number) RuleEngine.applyRule(rule, col("amount", "DECIMAL(12,2)"))).doubleValue() > 1000) {
                over++;
            }
        }
        double share = over * 100.0 / total;
        assertTrue(share > 26 && share < 34, "overThreshold actual " + share);
    }

    @Test
    void enumWeightsDistribution() {
        FieldRule rule = new FieldRule();
        rule.setType(FieldRule.TYPE_ENUM);
        rule.setValues(List.of("PENDING", "PAID", "DONE"));
        rule.setWeights(List.of(10.0, 80.0, 10.0));
        int paid = 0, total = 20_000;
        for (int i = 0; i < total; i++) {
            if ("PAID".equals(RuleEngine.applyRule(rule, col("status", "VARCHAR(16)")))) {
                paid++;
            }
        }
        double share = paid * 100.0 / total;
        assertTrue(share > 77 && share < 83, "weights actual " + share);
    }

    @Test
    void inlineEnumParsed() {
        assertEquals(List.of("NEW", "PAID", "CANCEL"),
                EnumValueDiscoverer.parseInlineEnum("ENUM('NEW','PAID','CANCEL')"));
        assertTrue(EnumValueDiscoverer.parseInlineEnum("VARCHAR(16)").isEmpty());
    }

    @Test
    void checkClauseParsed() {
        assertEquals(List.of("a", "b"), EnumValueDiscoverer.parseCheckClause("(`status` in ('a','b'))"));
        assertEquals(List.of(1L, 2L, 3L), EnumValueDiscoverer.parseCheckClause("status IN (1,2,3)"));
    }

    @Test
    void commentPairsParsed() {
        assertEquals(List.of(1L, 2L, 3L), EnumValueDiscoverer.parseComment(
                "状态 1=待支付 2=已支付 3=已取消", col("status", "TINYINT")));
        assertTrue(EnumValueDiscoverer.parseComment("普通注释", col("remark", "VARCHAR")).isEmpty());
    }

    @Test
    void javaEnumMatchedByName() {
        assertEquals(List.of("NEW", "PAID"), EnumValueDiscoverer.matchJavaEnum("order_status",
                Map.of("OrderStatus", List.of("NEW", "PAID"))));
        assertTrue(EnumValueDiscoverer.matchJavaEnum("remark",
                Map.of("OrderStatus", List.of("NEW"))).isEmpty());
    }

    @Test
    void namingInferenceAndEmptyFallback() {
        SchemaMetadata user = table("user");
        SchemaMetadata.ColumnMeta pk = col("id", "BIGINT");
        pk.setPrimaryKey(true);
        pk.setAutoIncrement(true);
        user.getColumns().add(pk);
        user.getPrimaryKeys().add("id");
        user.setRowCount(0); // 空表

        SchemaMetadata order = table("t_order");
        SchemaMetadata.ColumnMeta fk = col("user_id", "BIGINT");
        fk.setAutoIncrement(false);
        order.getColumns().add(fk);

        Map<String, SchemaMetadata> parents = new LinkedHashMap<>();
        parents.put("user", user);
        List<RelationHint> hints = new NamingRelationResolver().resolve(order, parents, null, null);
        // conn=null → 纯命名 0.5；空表兜底路径需要 dialect（此处覆盖匹配逻辑）
        assertEquals(1, hints.size());
        assertEquals("user", hints.get(0).getParentTable());
        assertEquals(RelationHint.SRC_NAMING, hints.get(0).getSource());
    }

    @Test
    void sqlLiteralsExtracted() {
        String sql = "SELECT * FROM perf_order o WHERE o.status = 'SHIPPED' AND o.user_id = 7";
        List<MagicHint> hints = SqlLiteralExtractor.extractLiterals(sql, "perf_order", "OrderMapper.java:20");
        assertTrue(hints.stream().anyMatch(h -> "status".equals(h.getColumn())
                && h.getValues().contains("SHIPPED")));
    }

    @Test
    void sqlJoinRelationsExtracted() {
        String sql = "SELECT o.* FROM perf_order o JOIN perf_user u ON o.user_id = u.id";
        List<RelationHint> hints = SqlLiteralExtractor.extractJoinRelations(sql,
                Set.of("perf_order", "perf_user"));
        assertEquals(1, hints.size());
        assertEquals("perf_order", hints.get(0).getChildTable());
        assertEquals("user_id", hints.get(0).getChildColumn());
    }

    @Test
    void malformedSqlSilentlySkipped() {
        String broken = "SELECT !!! FROM ((( WHERE";
        assertTrue(SqlLiteralExtractor.extractLiterals(broken, "t", "x").isEmpty());
        assertTrue(SqlLiteralExtractor.extractJoinRelations(broken, Set.of("t")).isEmpty());
    }
}

