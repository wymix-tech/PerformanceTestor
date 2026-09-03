package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 完整性预检单测。
 *
 * <p>核心场景来自评审发现的静默失效缺口：父表不在 tables 清单时，引擎
 * {@code ExplicitRuleHints} 会被 metaMap 过滤掉 explicit 边，ref 池为空后
 * 生成逻辑退化为随机数，数据毫无关联而全流程无任何报错。</p>
 */
class PlanIntegrityCheckerTest {

    private final PlanIntegrityChecker checker = new PlanIntegrityChecker(new ObjectMapper());

    private static final String CHILD_FIELDS =
            "{\"user_id\":{\"type\":\"ref\",\"table\":\"perf_user\",\"column\":\"id\"}}";
    private static final String SKEW_FIELDS =
            "{\"user_id\":{\"type\":\"refSkew\",\"table\":\"perf_user\",\"column\":\"id\","
                    + "\"hotPercent\":20,\"hotShare\":80}}";

    private static PlanIntegrityChecker.PlanTable table(String name, int rows, String fields) {
        PlanIntegrityChecker.PlanTable t = new PlanIntegrityChecker.PlanTable(name, rows, fields, "READ");
        return t;
    }

    @Test
    void missingParentIsAddedToTableList() {
        var out = checker.completeParents(List.of(table("perf_order", 10000, CHILD_FIELDS)));
        assertEquals(2, out.getTables().size(), "被 ref 的父表必须补入清单，否则 ref 池为空且无报错");
        assertEquals("perf_user", out.getTables().get(1).getTable());
    }

    @Test
    void addingParentProducesWarning() {
        var out = checker.completeParents(List.of(table("perf_order", 10000, CHILD_FIELDS)));
        assertTrue(out.getWarnings().stream().anyMatch(w -> w.contains("perf_user")),
                "补父表必须给出说明: " + out.getWarnings());
    }

    @Test
    void parentRowCountFollowsChildrenRatio() {
        var out = checker.completeParents(List.of(table("perf_order", 10000, CHILD_FIELDS)));
        assertEquals(RowCountEstimator.parentRows(10000), out.getTables().get(1).getRowCount());
    }

    @Test
    void refSkewParentIsAlsoCompleted() {
        var out = checker.completeParents(List.of(table("perf_order", 5000, SKEW_FIELDS)));
        assertEquals(2, out.getTables().size());
    }

    @Test
    void existingParentIsNotDuplicated() {
        var out = checker.completeParents(List.of(
                table("perf_order", 10000, CHILD_FIELDS),
                table("perf_user", 1000, null)));
        assertEquals(2, out.getTables().size(), "父表已在清单内时不得重复添加");
        assertTrue(out.getWarnings().isEmpty());
    }

    @Test
    void parentIsMarkedAsReadAccess() {
        var out = checker.completeParents(List.of(table("perf_order", 10000, CHILD_FIELDS)));
        assertEquals("READ", out.getTables().get(1).getAccess());
    }

    @Test
    void refTargetsAreExtracted() {
        assertEquals(List.of("perf_user"), checker.refTargets(CHILD_FIELDS));
        assertEquals(List.of("perf_user"), checker.refTargets(SKEW_FIELDS));
        assertTrue(checker.refTargets("{\"amount\":{\"type\":\"range\"}}").isEmpty());
        assertTrue(checker.refTargets(null).isEmpty());
        assertTrue(checker.refTargets("not json").isEmpty());
    }

    @Test
    void cycleNodesAreParsedFromEngineMessage() {
        assertEquals(List.of("a", "b"), checker.parseCycle("表组存在循环依赖，无法确定生成顺序: a -> b -> a"));
        assertEquals(List.of("a", "b"), checker.parseCycle("a->b->a"));
    }

    @Test
    void weakestCycleEdgeIsRemoved() {
        List<PlanIntegrityChecker.PlanTable> tables = new java.util.ArrayList<>(List.of(
                table("a", 100, "{\"b_id\":{\"type\":\"ref\",\"table\":\"b\",\"column\":\"id\"}}"),
                table("b", 100, "{\"a_id\":{\"type\":\"ref\",\"table\":\"a\",\"column\":\"id\"}}")));
        Map<String, Double> conf = Map.of("a->b", 0.4, "b->a", 0.9);
        boolean removed = checker.breakWeakestCycleEdge(tables, "a -> b -> a", conf);
        assertTrue(removed, "应剔除置信度最低的一条边以打破环");
        assertTrue(checker.refTargets(tables.get(0).getFields()).isEmpty(),
                "a->b 边（置信度 0.4）应被剔除");
        assertFalse(checker.refTargets(tables.get(1).getFields()).isEmpty(),
                "b->a 边（置信度 0.9）应保留");
    }

    // ===== 边置信度提取（PR 复审：此前调用方传 Map.of()，「最低置信度」退化为任选一条边） =====

    @Test
    void edgeConfidenceIsReadFromRuleJson() {
        List<PlanIntegrityChecker.PlanTable> tables = List.of(
                table("a", 100, "{\"b_id\":{\"type\":\"ref\",\"table\":\"b\",\"column\":\"id\","
                        + "\"confidence\":0.4}}"),
                table("b", 100, "{\"a_id\":{\"type\":\"ref\",\"table\":\"a\",\"column\":\"id\","
                        + "\"confidence\":0.9}}"));
        Map<String, Double> conf = checker.edgeConfidence(tables);
        assertEquals(0.4, conf.get("a->b"), 1e-9);
        assertEquals(0.9, conf.get("b->a"), 1e-9);
    }

    @Test
    void edgeConfidenceFallsBackToDefaultWhenAbsent() {
        List<PlanIntegrityChecker.PlanTable> tables = List.of(
                table("a", 100, "{\"b_id\":{\"type\":\"ref\",\"table\":\"b\",\"column\":\"id\"}}"));
        assertEquals(PlanIntegrityChecker.DEFAULT_EDGE_CONFIDENCE,
                checker.edgeConfidence(tables).get("a->b"), 1e-9);
    }

    @Test
    void edgeConfidenceIsCaseInsensitiveAndSkipsNonRefRules() {
        List<PlanIntegrityChecker.PlanTable> tables = List.of(
                table("Perf_Order", 100, "{\"user_id\":{\"type\":\"ref\",\"table\":\"Perf_User\","
                        + "\"column\":\"id\",\"confidence\":0.7},"
                        + "\"status\":{\"type\":\"enum\",\"values\":[1,2]}}"));
        Map<String, Double> conf = checker.edgeConfidence(tables);
        assertEquals(1, conf.size(), "非 ref 规则不产生边: " + conf);
        assertEquals(0.7, conf.get("perf_order->perf_user"), 1e-9,
                "边 key 统一小写，与环消息解析一致");
    }

    @Test
    void weakestEdgeIsChosenByRealConfidenceNotByOrder() {
        // 关键回归：置信度来自规则 JSON 时，被剔除的必须真的是最弱的那条边，
        // 而不是环中的第一条边（此处 a->b 排在前但置信度更高，必须保留）
        List<PlanIntegrityChecker.PlanTable> tables = new java.util.ArrayList<>(List.of(
                table("a", 100, "{\"b_id\":{\"type\":\"ref\",\"table\":\"b\",\"column\":\"id\","
                        + "\"confidence\":0.9}}"),
                table("b", 100, "{\"a_id\":{\"type\":\"ref\",\"table\":\"a\",\"column\":\"id\","
                        + "\"confidence\":0.5}}")));
        boolean removed = checker.breakWeakestCycleEdge(tables, "a -> b -> a",
                checker.edgeConfidence(tables));
        assertTrue(removed);
        assertFalse(checker.refTargets(tables.get(0).getFields()).isEmpty(),
                "a->b（0.9）应保留，尽管它在环消息中排在前面");
        assertTrue(checker.refTargets(tables.get(1).getFields()).isEmpty(),
                "b->a（0.5）应被剔除");
    }

    @Test
    void edgeConfidenceOfEmptyInputIsEmpty() {
        assertTrue(checker.edgeConfidence(null).isEmpty());
        assertTrue(checker.edgeConfidence(List.of()).isEmpty());
        assertTrue(checker.edgeConfidence(List.of(table("a", 1, null))).isEmpty());
        assertTrue(checker.edgeConfidence(List.of(table("a", 1, "not json"))).isEmpty());
    }

    @Test
    void breakCycleReturnsFalseWhenMessageUnparsable() {
        assertFalse(checker.breakWeakestCycleEdge(List.of(table("a", 1, null)), null, null));
        assertFalse(checker.breakWeakestCycleEdge(List.of(), "a -> b", null));
    }

    @Test
    void emptyInputIsSafe() {
        assertTrue(checker.completeParents(List.of()).getTables().isEmpty());
        assertTrue(checker.completeParents(null).getTables().isEmpty());
    }

    @Test
    void retryLimitIsOne() {
        assertEquals(1, PlanIntegrityChecker.MAX_CYCLE_RETRY, "环依赖只重试一次，避免无限重试");
    }
}
