package com.perftestor.datagen.util;

import com.perftestor.datagen.model.EndpointTableFact;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** WHERE 谓词抽取单测：等值 / IN / 范围三类，驱动数据构成推导。 */
class SqlPredicateExtractorTest {

    private static List<EndpointTableFact.Predicate> preds(String sql, String table) {
        return SqlPredicateExtractor.extract(sql, table);
    }

    private static EndpointTableFact.Predicate first(String sql, String table) {
        List<EndpointTableFact.Predicate> ps = preds(sql, table);
        assertTrue(!ps.isEmpty(), "期望抽出谓词: " + sql);
        return ps.get(0);
    }

    @Test
    void equalityWithNumericLiteral() {
        String sql = "select * from `order` o where o.status = 1";
        EndpointTableFact.Predicate p = first(sql, "order");
        assertEquals("status", p.getColumn());
        assertEquals(EndpointTableFact.Op.EQ, p.getOp());
        assertEquals(List.of(1L), p.getValues());
    }

    @Test
    void equalityWithStringLiteral() {
        String sql = "select * from `order` o where o.status = 'PAID'";
        EndpointTableFact.Predicate p = first(sql, "order");
        assertEquals(EndpointTableFact.Op.EQ, p.getOp());
        assertEquals(List.of("PAID"), p.getValues());
    }

    @Test
    void multipleEqualityOnSameColumnMergesIntoValueSet() {
        String sql = "select * from `order` o where o.status = 1 or o.status = 2";
        EndpointTableFact.Predicate p = first(sql, "order");
        assertEquals(EndpointTableFact.Op.EQ, p.getOp());
        assertEquals(List.of(1L, 2L), p.getValues(), "同列多等值应合并为值集");
    }

    @Test
    void inListIsExtracted() {
        String sql = "select * from `order` o where o.status in (1, 2, 3)";
        EndpointTableFact.Predicate p = first(sql, "order");
        assertEquals(EndpointTableFact.Op.IN, p.getOp());
        assertEquals(List.of(1L, 2L, 3L), p.getValues());
    }

    @Test
    void inListWithStringsIsExtracted() {
        String sql = "select * from `order` o where o.status in ('NEW', 'PAID')";
        EndpointTableFact.Predicate p = first(sql, "order");
        assertEquals(List.of("NEW", "PAID"), p.getValues());
    }

    @Test
    void rangeGreaterThanSetsMin() {
        String sql = "select * from `order` o where o.amount > 1000";
        EndpointTableFact.Predicate p = first(sql, "order");
        assertEquals(EndpointTableFact.Op.RANGE, p.getOp());
        assertEquals(1000.0, p.getMin());
    }

    @Test
    void rangeLessThanSetsMax() {
        String sql = "select * from `order` o where o.amount <= 50";
        EndpointTableFact.Predicate p = first(sql, "order");
        assertEquals(EndpointTableFact.Op.RANGE, p.getOp());
        assertEquals(50.0, p.getMax());
    }

    @Test
    void betweenSetsBothBounds() {
        String sql = "select * from `order` o where o.amount between 100 and 5000";
        EndpointTableFact.Predicate p = first(sql, "order");
        assertEquals(EndpointTableFact.Op.RANGE, p.getOp());
        assertEquals(100.0, p.getMin());
        assertEquals(5000.0, p.getMax());
    }

    @Test
    void predicatesOfOtherAliasAreNotAttributedToTargetTable() {
        String sql = "select * from `order` o join user u on o.user_id = u.id where u.type = 9";
        assertTrue(preds(sql, "order").isEmpty(), "别的表/别名的谓词不能算到本表头上");
        assertEquals("type", first(sql, "user").getColumn());
    }

    @Test
    void barePredicatesAreAcceptedWhenNoAliasExists() {
        String sql = "select * from `order` where status = 1";
        EndpointTableFact.Predicate p = first(sql, "order");
        assertEquals("status", p.getColumn());
        assertEquals(List.of(1L), p.getValues());
    }

    @Test
    void placeholderPredicatesYieldNothing() {
        String sql = "select * from `order` o where o.status = #{status}";
        assertTrue(preds(sql, "order").isEmpty(), "占位符谓词无数据构造价值，应为空");
    }

    @Test
    void blankInputYieldsNothing() {
        assertTrue(preds(null, "order").isEmpty());
        assertTrue(preds("select * from `order`", null).isEmpty());
    }

    @Test
    void joinOnConditionIsNotMistakenForPredicate() {
        String sql = "select * from `order` o join user u on o.user_id = u.id";
        assertTrue(preds(sql, "order").isEmpty(), "JOIN ON 不是 WHERE 谓词");
    }
}
