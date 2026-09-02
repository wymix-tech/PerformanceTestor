package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perftestor.datagen.model.EndpointTableFact;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 规则合成单测：谓词转枚举与权重、值集上限、seq 只落 prefix、ref/refSkew。 */
class PlanRuleComposerTest {

    private final PlanRuleComposer composer = new PlanRuleComposer();
    private final ObjectMapper mapper = new ObjectMapper();

    private static EndpointTableFact fact(String table, EndpointTableFact.SqlAccess access) {
        return new EndpointTableFact(table, access);
    }

    private static EndpointTableFact.Predicate eq(String col, Object... values) {
        EndpointTableFact.Predicate p = new EndpointTableFact.Predicate();
        p.setColumn(col);
        p.setOp(EndpointTableFact.Op.EQ);
        for (Object v : values) {
            p.getValues().add(v);
        }
        return p;
    }

    private static EndpointTableFact.Predicate range(String col, Double min, Double max) {
        EndpointTableFact.Predicate p = new EndpointTableFact.Predicate();
        p.setColumn(col);
        p.setOp(EndpointTableFact.Op.RANGE);
        p.setMin(min);
        p.setMax(max);
        return p;
    }

    private JsonNode fieldsOf(String json) throws Exception {
        return mapper.readTree(json);
    }

    @Test
    void equalityPredicateBecomesEnumWithValues() throws Exception {
        EndpointTableFact f = fact("perf_order", EndpointTableFact.SqlAccess.READ);
        f.getPredicates().add(eq("status", 1L, 2L));
        var out = composer.compose(List.of(f), Map.of());
        JsonNode fields = fieldsOf(out.getFieldsByTable().get("perf_order"));
        assertEquals("enum", fields.get("status").get("type").asText());
        assertEquals(2, fields.get("status").get("values").size());
    }

    @Test
    void weightsSumToHundred() throws Exception {
        EndpointTableFact f = fact("perf_order", EndpointTableFact.SqlAccess.READ);
        f.getPredicates().add(eq("status", 1L, 2L, 3L, 4L));
        var out = composer.compose(List.of(f), Map.of());
        JsonNode rule = fieldsOf(out.getFieldsByTable().get("perf_order")).get("status");
        double sum = 0;
        for (JsonNode w : rule.get("weights")) {
            sum += w.asDouble();
        }
        assertEquals(100.0, sum, 0.01, "权重之和必须为 100");
    }

    @Test
    void hitValuesTakeEightyPercentWhenSupplementsExist() throws Exception {
        EndpointTableFact f = fact("perf_order", EndpointTableFact.SqlAccess.READ);
        f.getPredicates().add(eq("status", 1L));
        PlanRuleComposer.TableContext ctx = new PlanRuleComposer.TableContext();
        // 枚举建议提供补充值：命中值 1 个占 80%，3 个补充值各占 20/3
        ctx.getEnumValues().put("status", List.of(1L, 2L, 3L, 4L));
        var out = composer.compose(List.of(f), Map.of("perf_order", ctx));
        JsonNode rule = fieldsOf(out.getFieldsByTable().get("perf_order")).get("status");
        assertEquals(PlanRuleComposer.HIT_WEIGHT_PERCENT, rule.get("weights").get(0).asDouble(), 0.01,
                "命中值（来自 SQL 谓词）应占 80%，保证接口查得到");
        double sum = 0;
        for (JsonNode w : rule.get("weights")) {
            sum += w.asDouble();
        }
        assertEquals(100.0, sum, 0.01);
    }

    @Test
    void predicateValuesComeFirstInValueSet() throws Exception {
        EndpointTableFact f = fact("perf_order", EndpointTableFact.SqlAccess.READ);
        f.getPredicates().add(eq("status", 7L));
        PlanRuleComposer.TableContext ctx = new PlanRuleComposer.TableContext();
        ctx.getEnumValues().put("status", List.of(1L, 2L));
        var out = composer.compose(List.of(f), Map.of("perf_order", ctx));
        JsonNode rule = fieldsOf(out.getFieldsByTable().get("perf_order")).get("status");
        assertEquals(7L, rule.get("values").get(0).asLong(), "谓词值必须排在最前（权重按位次分配）");
        assertEquals(3, rule.get("values").size(), "补充值应追加而非丢弃");
    }

    @Test
    void rangePredicateBecomesRangeRule() throws Exception {
        EndpointTableFact f = fact("perf_order", EndpointTableFact.SqlAccess.READ);
        f.getPredicates().add(range("amount", 100.0, 5000.0));
        var out = composer.compose(List.of(f), Map.of());
        JsonNode rule = fieldsOf(out.getFieldsByTable().get("perf_order")).get("amount");
        assertEquals("range", rule.get("type").asText());
        assertEquals(100.0, rule.get("min").asDouble(), 0.001);
        assertEquals(5000.0, rule.get("max").asDouble(), 0.001);
    }

    @Test
    void numericValueSetOverCapBecomesRange() throws Exception {
        EndpointTableFact f = fact("perf_order", EndpointTableFact.SqlAccess.READ);
        EndpointTableFact.Predicate p = eq("status");
        for (int i = 0; i < 100; i++) {
            p.getValues().add((long) i);
        }
        p.setOp(EndpointTableFact.Op.IN);
        f.getPredicates().add(p);
        var out = composer.compose(List.of(f), Map.of());
        JsonNode rule = fieldsOf(out.getFieldsByTable().get("perf_order")).get("status");
        assertEquals("range", rule.get("type").asText(), "纯数值超上限应转 range");
        assertTrue(out.getWarnings().stream().anyMatch(w -> w.contains("超过上限")),
                "值集被裁剪必须告警: " + out.getWarnings());
    }

    @Test
    void mixedValueSetOverCapIsSampled() throws Exception {
        EndpointTableFact f = fact("perf_order", EndpointTableFact.SqlAccess.READ);
        EndpointTableFact.Predicate p = eq("code");
        for (int i = 0; i < 200; i++) {
            p.getValues().add("V" + i);
        }
        p.setOp(EndpointTableFact.Op.IN);
        f.getPredicates().add(p);
        var out = composer.compose(List.of(f), Map.of());
        JsonNode rule = fieldsOf(out.getFieldsByTable().get("perf_order")).get("code");
        assertTrue(rule.get("values").size() <= PlanRuleComposer.MAX_ENUM_VALUES,
                "混合类型超上限应采样保留，实际: " + rule.get("values").size());
    }

    @Test
    void evenSampleKeepsBothEnds() {
        List<Object> values = new ArrayList<>();
        for (int i = 0; i < 200; i++) {
            values.add("V" + i);
        }
        List<Object> sampled = PlanRuleComposer.evenSample(values);
        assertEquals("V0", sampled.get(0));
        assertEquals("V199", sampled.get(sampled.size() - 1));
        assertTrue(sampled.size() <= PlanRuleComposer.MAX_ENUM_VALUES);
    }

    @Test
    void uniqueColumnYieldsSeqWithPrefixOnly() throws Exception {
        EndpointTableFact f = fact("perf_order", EndpointTableFact.SqlAccess.WRITE);
        PlanRuleComposer.TableContext ctx = new PlanRuleComposer.TableContext();
        ctx.getUniqueColumns().add("order_no");
        var out = composer.compose(List.of(f), Map.of("perf_order", ctx));
        JsonNode rule = fieldsOf(out.getFieldsByTable().get("perf_order")).get("order_no");
        assertEquals("seq", rule.get("type").asText());
        assertTrue(rule.has("seqPrefix"));
        assertFalse(rule.has("seqStart"),
                "方案里只能存 seqPrefix，seqStart 固化后重复执行会撞唯一键");
    }

    @Test
    void seqPrefixDerivedFromColumnName() {
        assertEquals("ON_", PlanRuleComposer.seqPrefix("order_no"));
        assertEquals("I_", PlanRuleComposer.seqPrefix("id"), "单段列名取首字母");
        assertEquals("S_", PlanRuleComposer.seqPrefix(""));
        assertEquals("S_", PlanRuleComposer.seqPrefix(null));
    }

    @Test
    void enumSuggestionsFillColumnsWithoutPredicates() throws Exception {
        EndpointTableFact f = fact("perf_order", EndpointTableFact.SqlAccess.READ);
        PlanRuleComposer.TableContext ctx = new PlanRuleComposer.TableContext();
        ctx.getEnumValues().put("status", List.of("NEW", "PAID"));
        var out = composer.compose(List.of(f), Map.of("perf_order", ctx));
        JsonNode rule = fieldsOf(out.getFieldsByTable().get("perf_order")).get("status");
        assertEquals("enum", rule.get("type").asText());
        assertEquals(2, rule.get("values").size());
    }

    @Test
    void predicateValueIsNotOverwrittenByEnumSuggestion() throws Exception {
        EndpointTableFact f = fact("perf_order", EndpointTableFact.SqlAccess.READ);
        f.getPredicates().add(eq("status", 1L));
        PlanRuleComposer.TableContext ctx = new PlanRuleComposer.TableContext();
        ctx.getEnumValues().put("status", List.of("NEW", "PAID"));
        var out = composer.compose(List.of(f), Map.of("perf_order", ctx));
        JsonNode rule = fieldsOf(out.getFieldsByTable().get("perf_order")).get("status");
        assertEquals(1L, rule.get("values").get(0).asLong(), "谓词值优先于枚举建议，保证接口能查到");
        assertTrue(rule.get("values").toString().contains("NEW"), "枚举建议应作为补充值保留");
    }

    @Test
    void refRuleUsesRefSkewForDetailLookup() {
        JsonNode plain = composer.refRule("user", "id", false);
        assertEquals("ref", plain.get("type").asText());
        JsonNode detail = composer.refRule("user", "id", true);
        assertEquals("refSkew", detail.get("type").asText());
        assertEquals(PlanRuleComposer.HOT_PERCENT, detail.get("hotPercent").asInt());
        assertEquals(PlanRuleComposer.HOT_SHARE, detail.get("hotShare").asInt());
    }

    @Test
    void duplicateValuesAreDeduplicated() throws Exception {
        EndpointTableFact f = fact("perf_order", EndpointTableFact.SqlAccess.READ);
        f.getPredicates().add(eq("status", 1L, 1L, 2L));
        var out = composer.compose(List.of(f), Map.of());
        JsonNode rule = fieldsOf(out.getFieldsByTable().get("perf_order")).get("status");
        assertEquals(2, rule.get("values").size());
    }

    @Test
    void emptyFactsYieldsNothing() {
        assertTrue(composer.compose(List.of(), Map.of()).getFieldsByTable().isEmpty());
        assertTrue(composer.compose(null, null).getFieldsByTable().isEmpty());
    }
}
