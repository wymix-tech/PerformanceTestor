package com.perftestor.datagen.engine;

import com.perftestor.datagen.engine.RelationResolver;
import com.perftestor.datagen.model.DataGenSpec;
import com.perftestor.datagen.model.FieldRule;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SchemaMetadata;
import com.perftestor.datagen.model.TableSpec;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 关联四级合并单测（纯对象，无 DB）：优先级/去重/显式忽略组外表/naming 待确认。 */
class RelationResolverMergeTest {

    private static SchemaMetadata table(String name) {
        SchemaMetadata m = new SchemaMetadata();
        m.setTableName(name);
        return m;
    }

    private static DataGenSpec specWithRef(String child, String col, String parent) {
        DataGenSpec spec = new DataGenSpec();
        TableSpec ts = new TableSpec(child, 10);
        FieldRule r = new FieldRule();
        r.setType(FieldRule.TYPE_REF);
        r.setTable(parent);
        r.setColumn("id");
        ts.getFields().put(col, r);
        spec.getTables().add(ts);
        return spec;
    }

    @Test
    void explicitOverridesDbFk() {
        SchemaMetadata order = table("t_order");
        order.getForeignKeys().add(new SchemaMetadata.FkMeta("user_id", "other", "id"));
        SchemaMetadata user = table("t_user");
        var metas = List.of(order, user);
        var r = new RelationResolver().resolve(metas, specWithRef("t_order", "user_id", "t_user"), null, null);
        assertEquals(1, r.getAutoEdges().size());
        assertEquals(RelationHint.SRC_EXPLICIT, r.getAutoEdges().get(0).getSource());
        assertEquals("t_user", r.getAutoEdges().get(0).getParentTable());
    }

    @Test
    void dbFkAutoApplied() {
        SchemaMetadata order = table("t_order");
        order.getForeignKeys().add(new SchemaMetadata.FkMeta("user_id", "t_user", "id"));
        var r = new RelationResolver().resolve(List.of(order, table("t_user")), new DataGenSpec(), null, null);
        assertEquals(1, r.getAutoEdges().size());
        assertEquals(RelationHint.SRC_DB_FK, r.getAutoEdges().get(0).getSource());
    }

    @Test
    void ormJoinAutoAppliedAndOverriddenByExplicit() {
        RelationResolver resolver = new RelationResolver();
        resolver.registerSource(List.of(new RelationHint("t_order", "user_id", "t_user", "id",
                RelationHint.SRC_ORM_JOIN, 0.9)));
        var r = resolver.resolve(List.of(table("t_order"), table("t_user")),
                specWithRef("t_order", "user_id", "t_user"), null, null);
        assertEquals(1, r.getAutoEdges().size());
        assertEquals(RelationHint.SRC_EXPLICIT, r.getAutoEdges().get(0).getSource());
    }

    @Test
    void namingFallsBackToPending() {
        SchemaMetadata order = table("t_order");
        SchemaMetadata.ColumnMeta fk = new SchemaMetadata.ColumnMeta();
        fk.setName("other_id");
        fk.setTypeName("BIGINT");
        order.getColumns().add(fk);
        SchemaMetadata other = table("other");
        SchemaMetadata.ColumnMeta pk = new SchemaMetadata.ColumnMeta();
        pk.setName("id");
        pk.setTypeName("BIGINT");
        pk.setPrimaryKey(true);
        other.getColumns().add(pk);
        other.getPrimaryKeys().add("id");
        var r = new RelationResolver().resolve(List.of(order, other), new DataGenSpec(), null, null);
        assertTrue(r.getAutoEdges().isEmpty());
        assertEquals(1, r.getPendingSuggestions().size());
        assertEquals(RelationHint.SRC_NAMING, r.getPendingSuggestions().get(0).getSource());
    }

    @Test
    void explicitRefToUnknownTableIgnored() {
        SchemaMetadata order = table("t_order");
        SchemaMetadata user = table("t_user");
        var r = new RelationResolver().resolve(List.of(order, user),
                specWithRef("t_order", "user_id", "ghost_table"), null, null);
        assertTrue(r.getAutoEdges().isEmpty());
        assertTrue(r.getPendingSuggestions().isEmpty());
    }

    @Test
    void namingSuffixMatching() {
        var tables = java.util.Set.of("t_user", "users");
        assertEquals("t_user", NamingRelationResolver.matchParentTable("t_user_id", "id", tables));
        assertEquals("users", NamingRelationResolver.matchParentTable("users_id", "id", tables));
        assertEquals(null, NamingRelationResolver.matchParentTable("amount", "id", tables));
        assertEquals(null, NamingRelationResolver.matchParentTable("_id", "id", tables));
        assertEquals("t_category", NamingRelationResolver.matchParentTable("t_category_id", "id", java.util.Set.of("t_category")));
    }
}
