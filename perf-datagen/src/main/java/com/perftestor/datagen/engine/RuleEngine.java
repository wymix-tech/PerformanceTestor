package com.perftestor.datagen.engine;

import com.perftestor.datagen.model.FieldRule;
import com.perftestor.datagen.model.SchemaMetadata;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;

/**
 * 规则引擎：字段值生成（fixed/range/enum+weights/ref/refSkew/overThreshold + 类型默认映射）。
 * 纯函数式（ref/refSkew 的采样池由引擎层注入），可独立单测。
 */
public final class RuleEngine {

    private RuleEngine() {
    }

    /** 列类型默认生成器（承接原 DataGenService.generateDefault 行为） */
    public static Object generateDefault(SchemaMetadata.ColumnMeta col) {
        ThreadLocalRandom r = ThreadLocalRandom.current();
        String type = col.getTypeName() == null ? "VARCHAR" : col.getTypeName().toUpperCase();
        String base = baseType(type);
        switch (base) {
            case "INT":
                return r.nextInt(1, 100000);
            case "BIGINT":
                return Math.abs(r.nextLong());
            case "FLOAT", "DOUBLE":
                return Math.round(r.nextDouble() * 10000) / 100.0;
            case "DECIMAL":
                return Math.round(r.nextDouble() * 100000) / 100.0;
            case "DATE", "DATETIME":
                return LocalDateTime.now().minusDays(r.nextInt(0, 365)).withNano(0);
            case "BIT":
                return r.nextInt(0, 2);
            case "TEXT":
                return randomText(Math.min(Math.max(col.getSize(), 1), 200));
            default:
                return randomText(Math.max(1, Math.min(col.getSize(), 64)));
        }
    }

    /** ENUM('A','B') 等复合类型名的基础类型提取 */
    public static String baseType(String typeName) {
        String t = typeName.toUpperCase();
        int paren = t.indexOf('(');
        String head = paren > 0 ? t.substring(0, paren).trim() : t.trim();
        if (head.equals("ENUM") || head.equals("SET")) {
            return "TEXT";
        }
        switch (head) {
            case "INT", "INTEGER", "SMALLINT", "TINYINT", "MEDIUMINT", "YEAR":
                return "INT";
            case "FLOAT", "REAL":
                return "FLOAT";
            case "DOUBLE", "DOUBLE PRECISION":
                return "DOUBLE";
            case "DECIMAL", "NUMERIC":
                return "DECIMAL";
            case "DATE":
                return "DATE";
            case "DATETIME", "TIMESTAMP":
                return "DATETIME";
            case "BIT", "BOOLEAN", "BOOL":
                return "BIT";
            case "TEXT", "MEDIUMTEXT", "LONGTEXT", "TINYTEXT", "CLOB":
                return "TEXT";
            default:
                return head; // VARCHAR/CHAR 等
        }
    }

    /** 是否整数族列（关联列类型兼容校验用） */
    public static boolean isIntegerFamily(String typeName) {
        String base = baseType(typeName);
        return base.equals("INT") || base.equals("BIGINT");
    }

    /**
     * 按规则生成一行该列的值（不涉及 ref/refSkew——两者由引擎从 ID 池采样后直接覆盖）。
     *
     * @return 转换后的 JDBC 友好类型（Integer/Long/BigDecimal/LocalDateTime/Boolean/String）
     */
    public static Object applyRule(FieldRule rule, SchemaMetadata.ColumnMeta col) {
        ThreadLocalRandom r = ThreadLocalRandom.current();
        String type = rule.getType() == null ? "" : rule.getType();
        switch (type) {
            case FieldRule.TYPE_FIXED: {
                Object v = rule.getValue();
                return normalizeScalar(v, col);
            }
            case FieldRule.TYPE_RANGE: {
                double min = rule.getMin() == null ? 0 : rule.getMin();
                double max = rule.getMax() == null ? 100 : rule.getMax();
                if (max < min) {
                    double t = min;
                    min = max;
                    max = t;
                }
                double picked;
                FieldRule.OverThreshold ot = rule.getOverThreshold();
                if (ot != null && r.nextDouble() * 100 < ot.getShare()) {
                    // 越过阈值区间：(value, max]（op=">"）或 [value, max]（op=">="）
                    double low = ">=".equals(ot.getOp()) ? ot.getValue() : Math.nextUp(ot.getValue());
                    picked = low + r.nextDouble() * Math.max(0, max - low);
                } else {
                    // 未越阈区间：[min, threshold]（阈值低于 min 时回退 [min, max]）
                    double high = ot != null && ot.getValue() > min ? ot.getValue() : max;
                    picked = min + r.nextDouble() * (high - min);
                }
                return scaleToColumnType(picked, col);
            }
            case FieldRule.TYPE_ENUM: {
                List<Object> values = rule.getValues();
                if (values == null || values.isEmpty()) {
                    return generateDefault(col);
                }
                List<Double> weights = rule.getWeights();
                int idx;
                if (weights != null && weights.size() == values.size()
                        && weights.stream().mapToDouble(Double::doubleValue).sum() > 0) {
                    idx = weightedIndex(weights, r);
                } else {
                    idx = r.nextInt(values.size());
                }
                return normalizeScalar(values.get(idx), col);
            }
            default:
                return generateDefault(col);
        }
    }

    /** 权重采样：累计分布 + 线性扫描（候选值通常 ≤ 32 个，无需二分） */
    static int weightedIndex(List<Double> weights, ThreadLocalRandom r) {
        double total = weights.stream().mapToDouble(Double::doubleValue).sum();
        double pick = r.nextDouble() * total;
        double acc = 0;
        for (int i = 0; i < weights.size(); i++) {
            acc += weights.get(i);
            if (pick < acc) {
                return i;
            }
        }
        return weights.size() - 1;
    }

    /** 标量归一化：数值列收 Number，布尔列收 0/1，其余转 String */
    static Object normalizeScalar(Object v, SchemaMetadata.ColumnMeta col) {
        if (v == null) {
            return null;
        }
        String base = baseType(col.getTypeName() == null ? "VARCHAR" : col.getTypeName());
        switch (base) {
            case "INT":
                if (v instanceof Number) {
                    return ((Number) v).intValue();
                }
                try {
                    return Integer.parseInt(String.valueOf(v).trim());
                } catch (NumberFormatException e) {
                    return v;
                }
            case "BIGINT":
                if (v instanceof Number) {
                    return ((Number) v).longValue();
                }
                try {
                    return Long.parseLong(String.valueOf(v).trim());
                } catch (NumberFormatException e) {
                    return v;
                }
            case "FLOAT", "DOUBLE", "DECIMAL":
                if (v instanceof Number) {
                    return scaleToColumnType(((Number) v).doubleValue(), col);
                }
                return v;
            case "BIT":
                if (v instanceof Number) {
                    return ((Number) v).intValue() != 0;
                }
                return Boolean.parseBoolean(String.valueOf(v));
            default:
                return String.valueOf(v);
        }
    }

    /** 数值按列类型定标：整数族取整，DECIMAL 保留 2 位，FLOAT/DOUBLE 保留 2 位 */
    static Object scaleToColumnType(double picked, SchemaMetadata.ColumnMeta col) {
        String base = baseType(col.getTypeName() == null ? "DECIMAL" : col.getTypeName());
        switch (base) {
            case "INT":
                return (int) Math.round(picked);
            case "BIGINT":
                return Math.round(picked);
            case "FLOAT", "DOUBLE":
                return Math.round(picked * 100) / 100.0;
            default:
                return BigDecimal.valueOf(picked).setScale(2, RoundingMode.HALF_UP);
        }
    }

    public static String randomText(int len) {
        String chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
        StringBuilder sb = new StringBuilder(len);
        ThreadLocalRandom r = ThreadLocalRandom.current();
        for (int i = 0; i < len; i++) {
            sb.append(chars.charAt(r.nextInt(chars.length())));
        }
        return sb.toString();
    }
}
