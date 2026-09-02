package com.perftestor.datagen.util;

import com.perftestor.datagen.model.EndpointTableFact;
import org.junit.jupiter.api.Test;

import java.util.List;

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
}
