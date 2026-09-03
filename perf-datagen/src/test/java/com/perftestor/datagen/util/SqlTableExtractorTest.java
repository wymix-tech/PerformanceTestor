package com.perftestor.datagen.util;

import com.perftestor.datagen.model.EndpointTableFact;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** SQL 表名与读写类型抽取单测：含宁缺勿滥的降级出口验证。 */
class SqlTableExtractorTest {

    private static List<String> tables(String sql) {
        return SqlTableExtractor.extract(sql).tableNames();
    }

    private static EndpointTableFact.SqlAccess accessOf(String sql, String table) {
        for (SqlTableExtractor.TableAccess t : SqlTableExtractor.extract(sql).getTables()) {
            if (t.getTable().equals(table)) {
                return t.getAccess();
            }
        }
        return null;
    }

    @Test
    void selectFromSingleTableIsRead() {
        assertEquals(List.of("order"), tables("select * from `order` o where o.status = 1"));
        assertEquals(EndpointTableFact.SqlAccess.READ, accessOf("select * from `order` o where o.status = 1", "order"));
    }

    @Test
    void joinTablesAreAllRead() {
        SqlTableExtractor.Result r = SqlTableExtractor.extract(
                "select o.id, u.name from `order` o join user u on o.user_id = u.id where o.status = 1");
        assertEquals(List.of("order", "user"), r.tableNames());
        assertEquals(EndpointTableFact.SqlAccess.READ, accessOf(
                "select o.id from `order` o join user u on o.user_id = u.id", "user"));
    }

    @Test
    void insertIntoIsWrite() {
        String sql = "insert into `order` (user_id, amount) values (#{userId}, #{amount})";
        assertEquals(List.of("order"), tables(sql));
        assertEquals(EndpointTableFact.SqlAccess.WRITE, accessOf(sql, "order"));
    }

    @Test
    void updateIsWrite() {
        String sql = "update `order` set status = 2 where id = #{id}";
        assertEquals(List.of("order"), tables(sql));
        assertEquals(EndpointTableFact.SqlAccess.WRITE, accessOf(sql, "order"));
    }

    @Test
    void deleteFromIsWriteNotRead() {
        String sql = "delete from `order` where id = #{id}";
        assertEquals(List.of("order"), tables(sql));
        assertEquals(EndpointTableFact.SqlAccess.WRITE, accessOf(sql, "order"));
    }

    @Test
    void schemaPrefixedTableNameIsTrimmed() {
        assertEquals(List.of("order"), tables("select * from shop_db.`order` o"));
    }

    @Test
    void subqueryProducesWarningInsteadOfSilence() {
        SqlTableExtractor.Result r = SqlTableExtractor.extract(
                "select * from (select id from `order`) t");
        assertNotNull(r.getWarning(), "子查询必须给出降级提示，不能静默");
        assertTrue(r.getWarning().contains("子查询"));
    }

    @Test
    void cteProducesWarning() {
        SqlTableExtractor.Result r = SqlTableExtractor.extract(
                "with recent as (select id from `order`) select * from recent");
        assertNotNull(r.getWarning());
        assertTrue(r.getWarning().contains("CTE"));
    }

    @Test
    void multiTableUpdateProducesWarning() {
        SqlTableExtractor.Result r = SqlTableExtractor.extract("update `order` o, user u set o.status = 1");
        assertNotNull(r.getWarning());
        assertTrue(r.getWarning().contains("多表 UPDATE"));
    }

    @Test
    void keywordsAreNeverTreatedAsTable() {
        SqlTableExtractor.Result r = SqlTableExtractor.extract(
                "select * from `order` where status = 1 order by id desc limit 20");
        assertEquals(List.of("order"), r.tableNames());
    }

    @Test
    void pageSizeIsExtractedFromLimitLiteral() {
        assertEquals(20, SqlTableExtractor.extract("select * from `order` limit 20").getPageSize());
        assertEquals(0, SqlTableExtractor.extract("select * from `order` limit #{size}").getPageSize());
    }

    @Test
    void blankSqlReturnsEmpty() {
        assertTrue(SqlTableExtractor.extract(null).getTables().isEmpty());
        assertTrue(SqlTableExtractor.extract("   ").getTables().isEmpty());
    }

    @Test
    void writeStatementWithoutTargetWarnsInsteadOfGuessing() {
        SqlTableExtractor.Result r = SqlTableExtractor.extract("update set status = 1 where id = 1");
        assertNotNull(r.getWarning(), "写语句未识别到目标表时必须告警，不得猜表");
    }

    @Test
    void invalidTokensAreRejected() {
        assertNull(SqlTableExtractor.sanitize("1abc"));
        assertNull(SqlTableExtractor.sanitize("`"));
        assertNull(SqlTableExtractor.sanitize("select"));
        assertEquals("order", SqlTableExtractor.sanitize("`shop`.`order`"));
    }

    @Test
    void neverThrowsOnBrokenSql() {
        SqlTableExtractor.Result r = SqlTableExtractor.extract("select from ((( where join ???");
        assertFalse(r.getTables().size() < 0);
    }

    @Test
    void joinOnEdgeDirectionFromFkNaming() {
        // o.user_id = u.id → perf_order.user_id 指向 perf_user.id
        List<SqlTableExtractor.JoinEdge> edges = SqlTableExtractor.extractJoinEdges(
                "select o.id, u.name from perf_order o join perf_user u on o.user_id = u.id");
        assertEquals(1, edges.size());
        SqlTableExtractor.JoinEdge e = edges.get(0);
        assertEquals("perf_order", e.getChildTable());
        assertEquals("user_id", e.getChildColumn());
        assertEquals("perf_user", e.getParentTable());
        assertEquals("id", e.getParentColumn());
        assertEquals(0.9, e.getConfidence(), 0.001, "外键命名是最强证据");
    }

    @Test
    void joinOnEdgeDirectionFromPkSide() {
        // 列名无 FK 线索时，靠 "= u.id" 的主键侧判定方向
        List<SqlTableExtractor.JoinEdge> edges = SqlTableExtractor.extractJoinEdges(
                "select * from perf_order o join perf_user u on o.owner = u.id");
        assertEquals(1, edges.size());
        assertEquals("perf_order", edges.get(0).getChildTable());
        assertEquals("perf_user", edges.get(0).getParentTable());
        assertEquals(0.7, edges.get(0).getConfidence(), 0.001);
    }

    @Test
    void reversedJoinIsHandled() {
        // join 子表、from 父表的写法，方向应反过来
        List<SqlTableExtractor.JoinEdge> edges = SqlTableExtractor.extractJoinEdges(
                "select * from perf_user u join perf_order o on o.user_id = u.id");
        assertEquals(1, edges.size());
        assertEquals("perf_order", edges.get(0).getChildTable(), "子表应是外键所在的表");
        assertEquals("perf_user", edges.get(0).getParentTable());
    }

    @Test
    void joinWithoutAliasIsSupported() {
        List<SqlTableExtractor.JoinEdge> edges = SqlTableExtractor.extractJoinEdges(
                "select * from perf_order join perf_user on perf_order.user_id = perf_user.id");
        assertEquals(1, edges.size());
        assertEquals("perf_order", edges.get(0).getChildTable());
    }

    @Test
    void noJoinOnYieldsNoEdges() {
        assertTrue(SqlTableExtractor.extractJoinEdges("select * from perf_order where status = 1").isEmpty());
        assertTrue(SqlTableExtractor.extractJoinEdges(null).isEmpty());
    }

    @Test
    void qualifiedColumnsAreMappedByTable() {
        Map<String, Set<String>> cols = SqlTableExtractor.extractColumnsByTable(
                "select o.id, o.status from perf_order o where o.user_id = 1 and o.amount > 10");
        assertTrue(cols.containsKey("perf_order"));
        assertTrue(cols.get("perf_order").contains("user_id"));
        assertTrue(cols.get("perf_order").contains("status"));
    }

    @Test
    void fkNamingMatchesTableVariants() {
        assertTrue(SqlTableExtractor.isFkColumn("user_id", "perf_user"), "去前缀后应能匹配");
        assertTrue(SqlTableExtractor.isFkColumn("order_id", "perf_order"));
        assertTrue(SqlTableExtractor.isFkColumn("user_id", "users"), "复数表名应能匹配");
        assertFalse(SqlTableExtractor.isFkColumn("status", "perf_user"));
        assertFalse(SqlTableExtractor.isFkColumn("user_id", "perf_order"));
    }

    @Test
    void brokenJoinSqlDoesNotThrow() {
        assertTrue(SqlTableExtractor.extractJoinEdges("select from join on ???").isEmpty());
        assertTrue(SqlTableExtractor.extractColumnsByTable("select from ???").isEmpty());
    }
}
