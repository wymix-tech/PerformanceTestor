package com.perftestor.datagen.engine;

import com.perftestor.datagen.dialect.mysql.MySQLDialect;
import com.perftestor.datagen.executor.JdbcBatchExecutor;
import com.perftestor.datagen.model.DataGenSpec;
import com.perftestor.datagen.model.EngineListener;
import com.perftestor.datagen.model.EngineResult;
import com.perftestor.datagen.model.FieldRule;
import com.perftestor.datagen.model.TableSpec;
import com.perftestor.datagen.DefaultDataGenEngine;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * seq 规则单测：唯一索引/业务编码列的单调无碰撞生成。
 *
 * <p>覆盖两条关键性质：</p>
 * <ol>
 *   <li>单次批量内取值两两不同（H2 唯一约束下能全部插入成功）</li>
 *   <li>seqStart 变化时取值区间整体平移——这是「方案可重复执行」的基础：
 *       宿主在每次执行前把 seqStart 刷新为 MAX(col)+1，即可避开历史数据</li>
 * </ol>
 */
class SeqRuleTest {

    static class H2Dialect extends MySQLDialect {
        @Override
        public boolean supports(String jdbcUrl) {
            return jdbcUrl.startsWith("jdbc:h2:");
        }

        @Override
        public String enrichJdbcUrl(String jdbcUrl) {
            return jdbcUrl;
        }
    }

    private static final String URL = "jdbc:h2:mem:seq" + System.nanoTime()
            + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1";

    @BeforeAll
    static void initDb() throws Exception {
        try (Connection c = DriverManager.getConnection(URL, "sa", ""); Statement st = c.createStatement()) {
            st.execute("CREATE TABLE perf_coupon (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
                    + "coupon_no VARCHAR(32) NOT NULL UNIQUE, amount DECIMAL(10,2))");
        }
    }

    private static FieldRule seq(String prefix, Long start) {
        FieldRule r = new FieldRule();
        r.setType(FieldRule.TYPE_SEQ);
        r.setSeqPrefix(prefix);
        r.setSeqStart(start);
        return r;
    }

    @Test
    void valueIsPrefixPlusStartPlusRowIndex() {
        assertEquals("ORD_1", RuleEngine.seqValue(seq("ORD_", 1L), 0));
        assertEquals("ORD_5", RuleEngine.seqValue(seq("ORD_", 1L), 4));
        assertEquals("ORD_100", RuleEngine.seqValue(seq("ORD_", 100L), 0));
    }

    @Test
    void nullPrefixAndStartFallBackSafely() {
        FieldRule r = new FieldRule();
        r.setType(FieldRule.TYPE_SEQ);
        assertEquals("1", RuleEngine.seqValue(r, 0));
        assertEquals("3", RuleEngine.seqValue(r, 2));
    }

    @Test
    void startShiftMovesWholeRangeWhichEnablesRepeatExecution() {
        Set<String> first = new HashSet<>();
        Set<String> second = new HashSet<>();
        for (long i = 0; i < 50; i++) {
            first.add(RuleEngine.seqValue(seq("C", 1L), i));
            second.add(RuleEngine.seqValue(seq("C", 51L), i));
        }
        assertTrue(java.util.Collections.disjoint(first, second),
                "seqStart 刷新到 MAX+1 后，新一批取值必须与历史批次完全不重叠");
    }

    @Test
    void applyRuleReturnsStringForVarcharColumn() {
        SchemaMetadataHolder col = new SchemaMetadataHolder();
        Object v = RuleEngine.applyRule(seq("ORD_", 7L), col.varchar("coupon_no", 32), 3);
        assertEquals("ORD_10", v);
    }

    @Test
    void applyRuleOnNumericColumnYieldsNumber() {
        SchemaMetadataHolder col = new SchemaMetadataHolder();
        Object v = RuleEngine.applyRule(seq("", 1L), col.bigint("seq_no"), 2);
        assertEquals(3L, v);
    }

    @Test
    void batchInsertOfSeqColumnNeverViolatesUniqueConstraint() throws Exception {
        DefaultDataGenEngine engine = new DefaultDataGenEngine(new H2Dialect(), new JdbcBatchExecutor(), null);

        // 第一批：seqStart=1
        long first = run(engine, 1L, 200);
        assertEquals(200, first);

        // 第二批：模拟重复执行——宿主执行前把 seqStart 刷新为 MAX+1
        long maxNo = maxCouponNo();
        assertTrue(maxNo >= 200, "首批应写入 200 行，实际最大序号: " + maxNo);
        long second = run(engine, maxNo + 1, 200);
        assertEquals(200, second, "重复执行（seqStart 已刷新）必须全部插入成功，不得撞唯一键");

        assertEquals(400, countRows());
    }

    private long run(DefaultDataGenEngine engine, long seqStart, int rows) throws Exception {
        DataGenSpec spec = new DataGenSpec();
        spec.setJdbcUrl(URL);
        spec.setDbUser("sa");
        spec.setDbPassword("");
        spec.setDryRun(false);

        TableSpec ts = new TableSpec("perf_coupon", rows);
        Map<String, FieldRule> fields = new LinkedHashMap<>();
        fields.put("coupon_no", seq("C", seqStart));
        ts.setFields(fields);
        spec.setTables(java.util.List.of(ts));

        EngineResult r = engine.execute(spec, new EngineListener() {
            @Override
            public void onProgress(long totalRows, long insertedRows, String currentTable) {
            }

            @Override
            public void onHints(java.util.List<com.perftestor.datagen.model.MagicHint> magicHints,
                                java.util.List<com.perftestor.datagen.model.RelationHint> relationHints) {
            }

            @Override
            public void onWarn(String message) {
            }
        });
        return r.getTableStats().isEmpty() ? 0 : r.getTableStats().get(0).getRows();
    }

    private long maxCouponNo() throws Exception {
        try (Connection c = DriverManager.getConnection(URL, "sa", ""); Statement st = c.createStatement()) {
            // coupon_no 形如 C123：去掉前缀后取数值最大值
            try (ResultSet rs = st.executeQuery(
                    "SELECT MAX(CAST(SUBSTRING(coupon_no, 2) AS BIGINT)) FROM perf_coupon")) {
                return rs.next() ? rs.getLong(1) : 0;
            }
        }
    }

    private long countRows() throws Exception {
        try (Connection c = DriverManager.getConnection(URL, "sa", ""); Statement st = c.createStatement()) {
            try (ResultSet rs = st.executeQuery("SELECT COUNT(*) FROM perf_coupon")) {
                return rs.next() ? rs.getLong(1) : 0;
            }
        }
    }

    /** 列元数据构造辅助（避免每个用例重复 setXXX） */
    private static final class SchemaMetadataHolder {
        com.perftestor.datagen.model.SchemaMetadata.ColumnMeta varchar(String name, int size) {
            com.perftestor.datagen.model.SchemaMetadata.ColumnMeta c = col(name, "VARCHAR");
            c.setSize(size);
            return c;
        }

        com.perftestor.datagen.model.SchemaMetadata.ColumnMeta bigint(String name) {
            return col(name, "BIGINT");
        }

        private com.perftestor.datagen.model.SchemaMetadata.ColumnMeta col(String name, String type) {
            com.perftestor.datagen.model.SchemaMetadata.ColumnMeta c =
                    new com.perftestor.datagen.model.SchemaMetadata.ColumnMeta();
            c.setName(name);
            c.setTypeName(type);
            c.setSize(64);
            c.setNullable(true);
            return c;
        }
    }
}
