package com.perftestor.datagen.engine;

import com.perftestor.datagen.dialect.mysql.MySQLDialect;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SchemaMetadata;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.Statement;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 命名推断数据抽样验证（H2 有数据场景）：包含率→置信度分级。 */
class NamingResolverSamplingTest {

    private static final String URL = "jdbc:h2:mem:ns" + System.nanoTime()
            + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1";
    private static final MySQLDialect DIALECT = new H2Dialect();

    static class H2Dialect extends MySQLDialect {
        @Override
        public boolean supports(String u) {
            return true;
        }

        @Override
        public String enrichJdbcUrl(String u) {
            return u;
        }
    }

    @BeforeAll
    static void init() throws Exception {
        try (Connection c = DriverManager.getConnection(URL, "sa", ""); Statement st = c.createStatement()) {
            st.execute("CREATE TABLE seller (id BIGINT AUTO_INCREMENT PRIMARY KEY)");
            st.execute("INSERT INTO seller (id) VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9),(10)");
            st.execute("INSERT INTO seller (id) SELECT id+10 FROM seller");
            st.execute("INSERT INTO seller (id) SELECT id+20 FROM seller");
            st.execute("INSERT INTO seller (id) SELECT id+40 FROM seller");
            st.execute("CREATE TABLE goods (id BIGINT AUTO_INCREMENT PRIMARY KEY, seller_id BIGINT)");
            // 子表 10 行全部引用有效父 ID（包含率 1.0）
            st.execute("INSERT INTO goods (seller_id) VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9),(10)");
        }
    }

    private static SchemaMetadata meta(String name, long rowCount) {
        SchemaMetadata m = new SchemaMetadata();
        m.setTableName(name);
        m.setRowCount(rowCount);
        SchemaMetadata.ColumnMeta pk = new SchemaMetadata.ColumnMeta();
        pk.setName("id");
        pk.setTypeName("BIGINT");
        pk.setPrimaryKey(true);
        pk.setAutoIncrement(true);
        m.getColumns().add(pk);
        m.getPrimaryKeys().add("id");
        return m;
    }

    @Test
    void highContainRateBoostsConfidence() throws Exception {
        SchemaMetadata goods = meta("goods", 10);
        SchemaMetadata.ColumnMeta fk = new SchemaMetadata.ColumnMeta();
        fk.setName("seller_id");
        fk.setTypeName("BIGINT");
        goods.getColumns().add(fk);
        Map<String, SchemaMetadata> parents = new LinkedHashMap<>();
        SchemaMetadata seller = meta("seller", 40);
        parents.put("seller", seller);
        try (Connection c = DriverManager.getConnection(URL, "sa", "")) {
            List<RelationHint> hints = new NamingRelationResolver()
                    .resolve(goods, parents, c, DIALECT);
            assertEquals(1, hints.size());
            RelationHint h = hints.get(0);
            assertEquals("seller", h.getParentTable());
            // 包含率 1.0 ≥ 95% → 置信度 0.85
            assertEquals(0.85, h.getConfidence(), 1e-9);
            assertTrue(h.getSourceRefs().stream().anyMatch(s -> s.contains("containRate=1.00")));
        }
    }

    @Test
    void noMatchYieldsNoHint() throws Exception {
        SchemaMetadata goods = meta("goods", 10);
        SchemaMetadata.ColumnMeta plain = new SchemaMetadata.ColumnMeta();
        plain.setName("amount");
        plain.setTypeName("BIGINT");
        goods.getColumns().add(plain);
        Map<String, SchemaMetadata> parents = new LinkedHashMap<>();
        parents.put("seller", meta("seller", 40));
        try (Connection c = DriverManager.getConnection(URL, "sa", "")) {
            List<RelationHint> hints = new NamingRelationResolver()
                    .resolve(goods, parents, c, DIALECT);
            assertTrue(hints.isEmpty());
        }
    }

    @Test
    void emptyParentGivesLowConfidenceFallback() throws Exception {
        SchemaMetadata goods = meta("goods2", 0);
        SchemaMetadata.ColumnMeta fk = new SchemaMetadata.ColumnMeta();
        fk.setName("seller_id");
        fk.setTypeName("BIGINT");
        goods.getColumns().add(fk);
        Map<String, SchemaMetadata> parents = new LinkedHashMap<>();
        parents.put("seller", meta("seller", 0)); // 空表
        try (Connection c = DriverManager.getConnection(URL, "sa", "")) {
            List<RelationHint> hints = new NamingRelationResolver()
                    .resolve(goods, parents, c, DIALECT);
            assertEquals(1, hints.size());
            assertEquals(RelationHint.SRC_NAMING_EMPTY, hints.get(0).getSource());
            assertEquals(0.3, hints.get(0).getConfidence(), 1e-9);
        }
    }
}
