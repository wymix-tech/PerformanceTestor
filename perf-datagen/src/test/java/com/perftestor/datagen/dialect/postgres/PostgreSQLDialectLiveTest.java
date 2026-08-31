package com.perftestor.datagen.dialect.postgres;

import com.perftestor.datagen.model.SchemaMetadata;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.Statement;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * PostgreSQL 方言「真实 PG」冒烟验证：覆盖 H2 PG 兼容模式缺失的
 * {@code pg_enum} 原生枚举发现、CHECK 约束枚举发现与 SERIAL 自增识别。
 *
 * <p>需要真实 PostgreSQL 实例，由环境变量 {@code PG_JDBC_URL} 指定
 * （默认 {@code jdbc:postgresql://127.0.0.1:5432/demo}，用户 postgres/postgres）。
 * 实例不可用时整类跳过（{@link Assumptions#assumeTrue}），不影响常规构建。
 */
class PostgreSQLDialectLiveTest {

    private static final String URL = System.getenv().getOrDefault("PG_JDBC_URL",
            "jdbc:postgresql://127.0.0.1:5432/demo");
    private static final String USER = System.getenv().getOrDefault("PG_USER", "postgres");
    private static final String PASSWORD = System.getenv().getOrDefault("PG_PASSWORD", "postgres");

    private static final PostgreSQLDialect DIALECT = new PostgreSQLDialect();

    private static Connection conn() throws Exception {
        return DriverManager.getConnection(DIALECT.enrichJdbcUrl(URL), USER, PASSWORD);
    }

    @BeforeAll
    static void prepare() throws Exception {
        Assumptions.assumeTrue(reachable(), "无可用 PostgreSQL 实例，跳过真实库冒烟");
        try (Connection c = conn(); Statement st = c.createStatement()) {
            // 原生枚举类型
            st.execute("DROP TABLE IF EXISTS e2e_pg_order");
            st.execute("DROP TYPE IF EXISTS order_status");
            st.execute("CREATE TYPE order_status AS ENUM ('NEW','PAID','SHIPPED','DONE')");
            // SERIAL 自增 + 原生枚举列 + CHECK 枚举列
            st.execute("CREATE TABLE e2e_pg_order ("
                    + "id BIGSERIAL PRIMARY KEY, "
                    + "status order_status, "
                    + "channel VARCHAR(16) CHECK (channel IN ('APP','WEB','WAP')), "
                    + "amount NUMERIC(12,2))");
        }
    }

    private static boolean reachable() {
        try (Connection c = conn()) {
            return c != null && !c.isClosed();
        } catch (Exception e) {
            return false;
        }
    }

    @Test
    void enrichesUrlAndReadsMetadata() throws Exception {
        String enriched = DIALECT.enrichJdbcUrl(URL);
        assertTrue(enriched.contains("reWriteBatchedInserts=true"), "URL 应追加 PG 批量参数");

        try (Connection c = conn()) {
            SchemaMetadata meta = DIALECT.fetchMetadata(c, "e2e_pg_order");
            assertFalse(meta.getColumns().isEmpty(), "应读到列");
            assertTrue(meta.getPrimaryKeys().contains("id"), "主键应为 id，实际 " + meta.getPrimaryKeys());
            assertNotNull(meta.findColumn("amount"));
        }
    }

    @Test
    void detectsSerialAutoIncrement() throws Exception {
        try (Connection c = conn()) {
            SchemaMetadata meta = DIALECT.fetchMetadata(c, "e2e_pg_order");
            SchemaMetadata.ColumnMeta id = meta.findColumn("id");
            assertNotNull(id);
            assertTrue(id.isAutoIncrement(),
                    "BIGSERIAL 默认值 nextval(...) 应被识别为自增（SERIAL 识别逻辑）");
        }
    }

    @Test
    void discoversNativeEnumViaPgEnum() throws Exception {
        try (Connection c = conn()) {
            List<String> vals = DIALECT.getEnumValues(c, "e2e_pg_order", "status");
            assertEquals(List.of("NEW", "PAID", "SHIPPED", "DONE"), vals,
                    "应通过 pg_enum 读出原生枚举常量（H2 PG 模式无法覆盖此分支）");
        }
    }

    @Test
    void discoversCheckConstraintEnum() throws Exception {
        try (Connection c = conn()) {
            List<String> vals = DIALECT.getEnumValues(c, "e2e_pg_order", "channel");
            assertEquals(List.of("APP", "WEB", "WAP"), vals,
                    "应通过 pg_get_constraintdef 解析 CHECK 约束枚举（H2 PG 模式无 pg_catalog）");
        }
    }

    @Test
    void countsRowsWithQuotedIdentifier() throws Exception {
        try (Connection c = conn(); Statement st = c.createStatement()) {
            st.execute("DELETE FROM e2e_pg_order");
            assertEquals(0, DIALECT.tableRowCount(c, "e2e_pg_order"));
            st.execute("INSERT INTO e2e_pg_order (status, channel, amount) VALUES ('NEW','APP',9.9)");
            assertEquals(1, DIALECT.tableRowCount(c, "e2e_pg_order"), "双引号标识符计数应正常");
        }
    }
}
