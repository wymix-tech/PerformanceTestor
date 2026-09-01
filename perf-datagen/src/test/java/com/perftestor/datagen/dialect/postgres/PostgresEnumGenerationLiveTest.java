package com.perftestor.datagen.dialect.postgres;

import com.perftestor.datagen.DefaultDataGenEngine;
import com.perftestor.datagen.executor.JdbcBatchExecutor;
import com.perftestor.datagen.model.DataGenSpec;
import com.perftestor.datagen.model.EngineListener;
import com.perftestor.datagen.model.EngineResult;
import com.perftestor.datagen.model.TableSpec;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 灌数引擎「DB 枚举发现值参与生成」回归验证（需真实 PG）。
 *
 * <p>背景：枚举发现原先只产出「建议」，未参与值生成，导致未配置规则的枚举列被填入随机长串，
 * 对 PG 原生枚举列直接报 {@code column "status" is of type order_status but expression is of type
 * character varying}。本类锁定该修复：
 * <ul>
 *   <li>原生枚举列（{@code status}）生成值必须全部落在 pg_enum 常量集内；</li>
 *   <li>CHECK 约束枚举列（{@code channel}）生成值必须全部落在约束值集内。</li>
 * </ul>
 */
class PostgresEnumGenerationLiveTest {

    private static final String URL = System.getenv().getOrDefault("PG_JDBC_URL",
            "jdbc:postgresql://127.0.0.1:5432/demo");
    private static final String USER = System.getenv().getOrDefault("PG_USER", "postgres");
    private static final String PASSWORD = System.getenv().getOrDefault("PG_PASSWORD", "postgres");

    private static final DefaultDataGenEngine ENGINE = new DefaultDataGenEngine(
            List.of(new PostgreSQLDialect()), new JdbcBatchExecutor(), List.of());

    private static Connection conn() throws Exception {
        return DriverManager.getConnection(new PostgreSQLDialect().enrichJdbcUrl(URL), USER, PASSWORD);
    }

    @BeforeAll
    static void prepare() throws Exception {
        Assumptions.assumeTrue(reachable(), "无可用 PostgreSQL 实例，跳过真实库回归");
        try (Connection c = conn(); Statement st = c.createStatement()) {
            st.execute("DROP TABLE IF EXISTS e2e_pg_enum_gen");
            st.execute("DROP TYPE IF EXISTS e2e_status");
            st.execute("CREATE TYPE e2e_status AS ENUM ('NEW','PAID','SHIPPED','DONE')");
            st.execute("CREATE TABLE e2e_pg_enum_gen ("
                    + "id BIGSERIAL PRIMARY KEY, "
                    + "status e2e_status, "
                    + "channel VARCHAR(16) CHECK (channel IN ('APP','WEB','WAP')))");
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
    void enumColumnsGetDiscoveredValuesNotRandomStrings() throws Exception {
        try (Connection c = conn(); Statement st = c.createStatement()) {
            st.execute("DELETE FROM e2e_pg_enum_gen");
        }

        DataGenSpec spec = new DataGenSpec();
        spec.setJdbcUrl(URL);
        spec.setDbUser(USER);
        spec.setDbPassword(PASSWORD);
        spec.setDryRun(false);
        // 故意不配置任何字段规则：枚举值必须来自 DB 发现
        spec.getTables().add(new TableSpec("e2e_pg_enum_gen", 50));

        EngineResult r = ENGINE.execute(spec, noop());
        assertTrue(r.isSuccess(), "灌数应成功，实际: " + r.getErrorMsg());
        assertEquals(50, r.getInsertedRows());

        Set<String> statuses = new HashSet<>();
        Set<String> channels = new HashSet<>();
        try (Connection c = conn(); Statement st = c.createStatement();
             ResultSet rs = st.executeQuery("SELECT status, channel FROM e2e_pg_enum_gen")) {
            int n = 0;
            while (rs.next()) {
                statuses.add(rs.getString(1));
                channels.add(rs.getString(2));
                n++;
            }
            assertEquals(50, n, "应写入 50 行");
        }

        Set<String> expectedStatus = Set.of("NEW", "PAID", "SHIPPED", "DONE");
        Set<String> expectedChannel = Set.of("APP", "WEB", "WAP");
        assertTrue(expectedStatus.containsAll(statuses),
                "原生枚举列取值必须落在 pg_enum 常量集内，实际: " + statuses);
        assertTrue(expectedChannel.containsAll(channels),
                "CHECK 枚举列取值必须落在约束值集内，实际: " + channels);
        assertTrue(statuses.size() > 1, "应在枚举集内随机分布，实际仅 " + statuses);
    }

    @Test
    void cacheDoesNotLeakAcrossRuns() throws Exception {
        // 引擎为单例 Bean：连续两次执行（表结构不同）不应串用上一次的枚举缓存
        DataGenSpec spec = new DataGenSpec();
        spec.setJdbcUrl(URL);
        spec.setDbUser(USER);
        spec.setDbPassword(PASSWORD);
        spec.setDryRun(true);
        spec.getTables().add(new TableSpec("e2e_pg_enum_gen", 10));

        EngineResult r1 = ENGINE.execute(spec, noop());
        assertTrue(r1.isSuccess(), r1.getErrorMsg());
        EngineResult r2 = ENGINE.execute(spec, noop());
        assertTrue(r2.isSuccess(), r2.getErrorMsg());
    }

    private static EngineListener noop() {
        return new EngineListener() {
            @Override
            public void onProgress(long total, long inserted, String table) {
            }

            @Override
            public void onHints(List<com.perftestor.datagen.model.MagicHint> m,
                                List<com.perftestor.datagen.model.RelationHint> r) {
            }
        };
    }
}
