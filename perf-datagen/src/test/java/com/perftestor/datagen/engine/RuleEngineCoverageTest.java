package com.perftestor.datagen.engine;

import com.perftestor.datagen.model.FieldRule;
import com.perftestor.datagen.model.SchemaMetadata;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 规则引擎覆盖补强：类型矩阵/归一化/定标/边界断言（杀 PIT 存活突变）。 */
class RuleEngineCoverageTest {

    private static SchemaMetadata.ColumnMeta col(String name, String type) {
        SchemaMetadata.ColumnMeta c = new SchemaMetadata.ColumnMeta();
        c.setName(name);
        c.setTypeName(type);
        c.setSize(64);
        c.setNullable(true);
        return c;
    }

    @Test
    void generateDefaultTypeMatrix() {
        assertInstanceOf(Integer.class, RuleEngine.generateDefault(col("a", "INT")));
        assertInstanceOf(Long.class, RuleEngine.generateDefault(col("b", "BIGINT")));
        assertInstanceOf(Double.class, RuleEngine.generateDefault(col("c", "DOUBLE")));
        assertInstanceOf(Double.class, RuleEngine.generateDefault(col("d", "DECIMAL(12,2)")));
        assertInstanceOf(LocalDateTime.class, RuleEngine.generateDefault(col("e", "DATETIME")));
        assertInstanceOf(Integer.class, RuleEngine.generateDefault(col("f", "BIT")));
        assertInstanceOf(String.class, RuleEngine.generateDefault(col("g", "TEXT")));
        assertInstanceOf(String.class, RuleEngine.generateDefault(col("h", "VARCHAR(10)")));
        // SET/REAL/TINYTEXT/CHAR 分支
        assertInstanceOf(String.class, RuleEngine.generateDefault(col("i", "SET('A','B')")));
        assertInstanceOf(Double.class, RuleEngine.generateDefault(col("j", "REAL")));
        assertInstanceOf(String.class, RuleEngine.generateDefault(col("k", "TINYTEXT")));
    }

    @Test
    void normalizeScalarMatrix() {
        assertEquals(42, RuleEngine.normalizeScalar("42", col("a", "INT")));
        assertEquals(42L, RuleEngine.normalizeScalar("42", col("b", "BIGINT")));
        assertEquals(true, RuleEngine.normalizeScalar(1, col("c", "BIT")));
        assertEquals(false, RuleEngine.normalizeScalar(0, col("d", "BOOLEAN")));
        BigDecimal d = (BigDecimal) RuleEngine.normalizeScalar(3.14159, col("e", "DECIMAL(12,2)"));
        assertEquals(0, d.compareTo(BigDecimal.valueOf(3.14)));
        assertEquals("txt", RuleEngine.normalizeScalar("txt", col("f", "VARCHAR(8)")));
    }

    @Test
    void scaleToColumnTypeMatrix() {
        assertEquals(5, ((Number) RuleEngine.scaleToColumnType(5.4, col("a", "INT"))).intValue());
        assertEquals(5L, ((Number) RuleEngine.scaleToColumnType(5.4, col("b", "BIGINT"))).longValue());
        assertEquals(5.43, ((Number) RuleEngine.scaleToColumnType(5.4321, col("c", "DOUBLE"))).doubleValue(), 1e-9);
    }

    @Test
    void isIntegerFamily() {
        assertTrue(RuleEngine.isIntegerFamily("INT"));
        assertTrue(RuleEngine.isIntegerFamily("BIGINT"));
        assertTrue(RuleEngine.isIntegerFamily("TINYINT(1)"));
        assertTrue(!RuleEngine.isIntegerFamily("VARCHAR(10)"));
        assertTrue(!RuleEngine.isIntegerFamily("DECIMAL(12,2)"));
    }

    @Test
    void enumNullValuesFallsBackToDefault() {
        FieldRule r = new FieldRule();
        r.setType(FieldRule.TYPE_ENUM);
        // values 缺省 → generateDefault（VARCHAR → String）；变异体会在此 NPE
        Object v = RuleEngine.applyRule(r, col("s", "VARCHAR(8)"));
        assertInstanceOf(String.class, v);
    }

    @Test
    void rangeRespectsMinAndMax() {
        FieldRule r = new FieldRule();
        r.setType(FieldRule.TYPE_RANGE);
        r.setMin(100.0);
        r.setMax(5000.0);
        for (int i = 0; i < 5000; i++) {
            double v = ((Number) RuleEngine.applyRule(r, col("amt", "DECIMAL(12,2)"))).doubleValue();
            // 杀 L97（min 边界）与 L109（over 分支上界 max+low）
            assertTrue(v >= 100.0, "低于下限: " + v);
            assertTrue(v <= 5000.0, "超上限: " + v);
        }
    }

    @Test
    void rangeMaxLessThanMinSwaps() {
        FieldRule r = new FieldRule();
        r.setType(FieldRule.TYPE_RANGE);
        r.setMin(5000.0);
        r.setMax(100.0);
        double v = ((Number) RuleEngine.applyRule(r, col("amt", "DECIMAL(12,2)"))).doubleValue();
        assertTrue(v >= 100.0 && v <= 5000.0);
    }

    @Test
    void weightedIndexZeroWeightNeverSelected() {
        List<Double> weights = List.of(10.0, 0.0, 90.0);
        // 杀 L144 边界（pick < acc → <=）：零权重值不得被选中
        for (int i = 0; i < 20000; i++) {
            int idx = RuleEngine.weightedIndex(weights, java.util.concurrent.ThreadLocalRandom.current());
            assertTrue(idx == 0 || idx == 2, "零权重索引被选中: " + idx);
        }
    }

    @Test
    void unknownRuleTypeFallsBackToDefault() {
        FieldRule r = new FieldRule();
        r.setType("mystery");
        assertInstanceOf(String.class, RuleEngine.applyRule(r, col("s", "VARCHAR(8)")));
    }

    @Test
    void fixedTypeNormalization() {
        FieldRule intFixed = FieldRule.fixed("7");
        assertEquals(7, ((Number) RuleEngine.applyRule(intFixed, col("n", "INT"))).intValue());
        FieldRule strFixed = FieldRule.fixed("PAID");
        assertEquals("PAID", RuleEngine.applyRule(strFixed, col("s", "VARCHAR(8)")));
    }
}
