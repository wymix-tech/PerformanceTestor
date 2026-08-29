package com.perftestor.datagen;

import com.perftestor.datagen.dialect.mysql.MySQLDialect;
import com.perftestor.datagen.executor.JdbcBatchExecutor;
import com.perftestor.datagen.model.DataGenSpec;
import com.perftestor.datagen.model.EngineListener;
import com.perftestor.datagen.model.EngineResult;
import com.perftestor.datagen.model.FieldRule;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.TableSpec;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 引擎端到端单测（H2 MySQL 模式）：编排/方言/批量执行/关联识别/空表兜底/错误路径。 */
class EngineH2Test {

    /** 测试方言：复用 MySQLDialect 全部实现，仅放开 URL 前缀（体现 SPI 可插拔） */
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

    static final DefaultDataGenEngine ENGINE =
            new DefaultDataGenEngine(new H2Dialect(), new JdbcBatchExecutor(), null);
    static final String URL = "jdbc:h2:mem:dg" + System.nanoTime()
            + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1";

    @BeforeAll
    static void initDb() throws Exception {
        try (Connection c = DriverManager.getConnection(URL, "sa", ""); Statement st = c.createStatement()) {
            st.execute("CREATE TABLE perf_user (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
                    + "name VARCHAR(64), age INT, score DOUBLE, vip BIT, created_at DATETIME)");
            st.execute("CREATE TABLE perf_order (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
                    + "user_id BIGINT NOT NULL, amount DECIMAL(12,2), status VARCHAR(16), "
                    + "remark TEXT, created_at DATETIME, "
                    + "CONSTRAINT fk_o_u FOREIGN KEY (user_id) REFERENCES perf_user(id))");
        }
    }

    private static DataGenSpec spec(boolean dryRun) {
        DataGenSpec spec = new DataGenSpec();
        spec.setJdbcUrl(URL);
        spec.setDbUser("sa");
        spec.setDbPassword("");
        spec.setDryRun(dryRun);
        return spec;
    }

    private static FieldRule ref(String table, String column) {
        FieldRule r = new FieldRule();
        r.setType(FieldRule.TYPE_REF);
        r.setTable(table);
        r.setColumn(column);
        return r;
    }

    private static int count(String sql) throws Exception {
        try (Connection c = DriverManager.getConnection(URL, "sa", "");
             var rs = c.createStatement().executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        }
    }

    @Test
    void dryRunValidatesWithoutInsert() throws Exception {
        DataGenSpec spec = spec(true);
        TableSpec user = new TableSpec("perf_user", 50);
        TableSpec order = new TableSpec("perf_order", 80);
        order.getFields().put("user_id", ref("perf_user", "id"));
        spec.getTables().add(user);
        spec.getTables().add(order);
        AtomicInteger progress = new AtomicInteger();
        EngineResult r = ENGINE.execute(spec, progressListener(progress));
        assertTrue(r.isSuccess(), r.getErrorMsg());
        assertEquals(0, r.getInsertedRows());
        assertEquals(2, r.getTableStats().size());
        assertTrue(progress.get() > 0);
        assertEquals(0, count("SELECT COUNT(*) FROM perf_user"));
    }

    @Test
    void realInsertFkValidAndTopoOrder() throws Exception {
        try (Connection c = DriverManager.getConnection(URL, "sa", ""); Statement st = c.createStatement()) {
            st.execute("DELETE FROM perf_order");
            st.execute("DELETE FROM perf_user");
        }
        DataGenSpec spec = spec(false);
        TableSpec order = new TableSpec("perf_order", 200);
        order.getFields().put("user_id", ref("perf_user", "id"));
        order.getFields().put("status", FieldRule.fixed("PAID"));
        spec.getTables().add(order); // 子表在前，验证拓扑排序重排
        spec.getTables().add(new TableSpec("perf_user", 100));
        EngineResult r = ENGINE.execute(spec, progressListener(new AtomicInteger()));
        assertTrue(r.isSuccess(), r.getErrorMsg());
        assertEquals(300, r.getInsertedRows());
        assertEquals(200, count("SELECT COUNT(*) FROM perf_order"));
        // 外键 100% 有效
        assertEquals(0, count("SELECT COUNT(*) FROM perf_order o "
                + "WHERE NOT EXISTS (SELECT 1 FROM perf_user u WHERE u.id = o.user_id)"));
        assertEquals(100, count("SELECT COUNT(*) FROM perf_user WHERE name IS NOT NULL"));
    }

    @Test
    void dbForeignKeyAutoDetected() throws Exception {
        // 无显式规则，靠声明外键自动识别（独立表避免计数串扰）
        try (Connection c = DriverManager.getConnection(URL, "sa", ""); Statement st = c.createStatement()) {
            st.execute("CREATE TABLE t_fk_p (id BIGINT AUTO_INCREMENT PRIMARY KEY, pname VARCHAR(32))");
            st.execute("CREATE TABLE t_fk_c (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
                    + "pid BIGINT NOT NULL, CONSTRAINT fk_cp FOREIGN KEY (pid) REFERENCES t_fk_p(id))");
        }
        DataGenSpec spec = spec(false);
        spec.getTables().add(new TableSpec("t_fk_p", 30));
        spec.getTables().add(new TableSpec("t_fk_c", 60));
        EngineResult r = ENGINE.execute(spec, progressListener(new AtomicInteger()));
        assertTrue(r.isSuccess(), r.getErrorMsg());
        assertEquals(0, count("SELECT COUNT(*) FROM t_fk_c c "
                + "WHERE NOT EXISTS (SELECT 1 FROM t_fk_p p WHERE p.id = c.pid)"));
    }

    @Test
    void emptyTableNamingFallbackDelivered() throws Exception {
        // 表名不带前缀，匹配 dept_id → dept 的命名推断
        try (Connection c = DriverManager.getConnection(URL, "sa", ""); Statement st = c.createStatement()) {
            st.execute("CREATE TABLE dept (id BIGINT AUTO_INCREMENT PRIMARY KEY, dname VARCHAR(32))");
            st.execute("CREATE TABLE emp (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
                    + "dept_id BIGINT, ename VARCHAR(32))");
        }
        DataGenSpec spec = spec(false);
        spec.getTables().add(new TableSpec("dept", 0));
        spec.getTables().add(new TableSpec("emp", 20));
        List<RelationHint> hints = new ArrayList<>();
        EngineResult r = ENGINE.execute(spec, progressListener(new AtomicInteger(), hints));
        assertTrue(r.isSuccess(), r.getErrorMsg());
        // 空表兜底：父表 0 行 → naming_empty_fallback（LOW，待确认）
        assertTrue(hints.stream().anyMatch(h ->
                RelationHint.SRC_NAMING_EMPTY.equals(h.getSource())
                        && "emp".equals(h.getChildTable()) && "dept_id".equals(h.getChildColumn())),
                "hints=" + hints);
    }

    @Test
    void orphanRateOnUnconstrainedTable() throws Exception {
        // 无外键约束的独立表：orphanRate=1.0 全部孤儿（NULL 或无效 ID）
        try (Connection c = DriverManager.getConnection(URL, "sa", ""); Statement st = c.createStatement()) {
            st.execute("CREATE TABLE t_orp_p (id BIGINT AUTO_INCREMENT PRIMARY KEY)");
            st.execute("CREATE TABLE t_orp_c (id BIGINT AUTO_INCREMENT PRIMARY KEY, pid BIGINT)");
        }
        DataGenSpec spec = spec(false);
        spec.getTables().add(new TableSpec("t_orp_p", 40));
        TableSpec child = new TableSpec("t_orp_c", 100);
        child.getFields().put("pid", ref("t_orp_p", "id"));
        child.setOrphanRate(1.0);
        spec.getTables().add(child);
        EngineResult r = ENGINE.execute(spec, progressListener(new AtomicInteger()));
        assertTrue(r.isSuccess(), r.getErrorMsg());
        // 有效引用应显著少于 100（部分 NULL/部分无效 ID）
        int valid = count("SELECT COUNT(*) FROM t_orp_c c "
                + "WHERE EXISTS (SELECT 1 FROM t_orp_p p WHERE p.id = c.pid)");
        assertTrue(valid < 100, "孤儿率 1.0 时有效引用应少于全量: " + valid);
    }

    @Test
    void badTableFailsFast() throws Exception {
        DataGenSpec spec = spec(true);
        spec.getTables().add(new TableSpec("nope_missing", 10));
        EngineResult r = ENGINE.execute(spec, progressListener(new AtomicInteger()));
        assertTrue(!r.isSuccess());
        assertTrue(r.getErrorMsg().contains("表不存在"));
    }

    @Test
    void cycleDependencyFails() {
        DataGenSpec spec = spec(true);
        TableSpec a = new TableSpec("perf_user", 10);
        a.getFields().put("user_id", ref("perf_order", "id"));
        TableSpec b = new TableSpec("perf_order", 10);
        b.getFields().put("user_id", ref("perf_user", "id"));
        spec.getTables().add(a);
        spec.getTables().add(b);
        EngineResult r = ENGINE.execute(spec, progressListener(new AtomicInteger()));
        assertTrue(!r.isSuccess());
        assertTrue(r.getErrorMsg().contains("循环依赖"));
    }

    @Test
    void unsupportedUrlRejected() {
        DataGenSpec spec = spec(true);
        spec.setJdbcUrl("jdbc:oracle:thin:@x");
        EngineResult r = ENGINE.execute(spec, progressListener(new AtomicInteger()));
        assertTrue(!r.isSuccess());
        assertTrue(r.getErrorMsg().contains("不支持"));
    }

    @Test
    void maxRowsExceededRejected() throws Exception {
        DataGenSpec spec = spec(true);
        spec.setMaxRows(10);
        spec.getTables().add(new TableSpec("perf_user", 100));
        EngineResult r = ENGINE.execute(spec, progressListener(new AtomicInteger()));
        assertTrue(!r.isSuccess());
        assertTrue(r.getErrorMsg().contains("总行数"));
    }

    private static EngineListener progressListener(AtomicInteger counter) {
        return progressListener(counter, new ArrayList<>());
    }

    private static EngineListener progressListener(AtomicInteger counter, List<RelationHint> hints) {
        return new EngineListener() {
            @Override
            public void onProgress(long total, long inserted, String table) {
                counter.incrementAndGet();
            }

            @Override
            public void onHints(List<com.perftestor.datagen.model.MagicHint> m, List<RelationHint> r) {
                hints.addAll(r);
            }
        };
    }
}
