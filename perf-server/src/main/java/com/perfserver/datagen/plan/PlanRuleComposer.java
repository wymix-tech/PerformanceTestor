package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perftestor.datagen.model.EndpointTableFact;
import com.perftestor.datagen.model.EndpointTableFact.Op;
import com.perftestor.datagen.model.EndpointTableFact.Predicate;
import com.perftestor.datagen.model.EndpointTableFact.SqlAccess;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * 规则合成器：接口访问事实 → 字段生成规则 JSON（喂给 perf-datagen 的 FieldRule）。
 *
 * <p>映射关系：</p>
 * <table border="1">
 *   <caption>信号 → 规则</caption>
 *   <tr><td>WHERE 等值 / IN</td><td>enum + values（谓词值优先）+ weights（命中值 80%，其余均分 20%）</td></tr>
 *   <tr><td>范围谓词</td><td>range(min,max)</td></tr>
 *   <tr><td>JOIN / 按 id 详情</td><td>ref；详情类改 refSkew（hotPercent 20 / hotShare 80）</td></tr>
 *   <tr><td>唯一索引 / 业务编码列</td><td>seq（<b>只落 seqPrefix</b>，seqStart 执行时刷新）</td></tr>
 * </table>
 *
 * <p>值集上限 {@value #MAX_ENUM_VALUES}：多步骤 union 或动态 IN 会产生超大值集，
 * 让权重分配失真（每个值的命中率被摊薄到失去区分度）。超限后纯数值转 range、
 * 混合类型均匀采样保留 64 个并写 warnings。</p>
 */
@Component
public class PlanRuleComposer {

    private static final Logger log = LoggerFactory.getLogger(PlanRuleComposer.class);

    /** 枚举值集上限 */
    public static final int MAX_ENUM_VALUES = 64;
    /** 命中值（来自 SQL 谓词）在权重中的总占比 */
    public static final int HIT_WEIGHT_PERCENT = 80;
    /** 详情类查询的热点占比 / 热点集中度（与 SuggestDataRulesTool 现有口径一致） */
    public static final int HOT_PERCENT = 20;
    public static final int HOT_SHARE = 80;

    /** ObjectMapper 线程安全且创建开销大，全局复用（本类无状态） */
    private static final com.fasterxml.jackson.databind.ObjectMapper MAPPER =
            new com.fasterxml.jackson.databind.ObjectMapper();

    /** 合成结果 */
    public static final class Composed {
        /** 表 → 字段规则 JSON（形如 {"col":{...}} 的字符串） */
        private final Map<String, String> fieldsByTable = new LinkedHashMap<>();
        private final List<String> warnings = new ArrayList<>();

        public Map<String, String> getFieldsByTable() {
            return fieldsByTable;
        }

        public List<String> getWarnings() {
            return warnings;
        }
    }

    /** 表上下文：外键引用边 + 唯一索引列 + 已有枚举建议值 + 是否详情类查询 */
    public static final class TableContext {
        private final List<com.perftestor.datagen.model.EndpointTableFact.JoinRef> joinRefs = new ArrayList<>();
        private final Set<String> uniqueColumns = new LinkedHashSet<>();
        private final Map<String, List<Object>> enumValues = new LinkedHashMap<>();
        private boolean detailLookup;

        /** 外键引用边（来自 JOIN ON 或外键命名），驱动 ref/refSkew 与父表补齐 */
        public List<com.perftestor.datagen.model.EndpointTableFact.JoinRef> getJoinRefs() {
            return joinRefs;
        }

        public Set<String> getUniqueColumns() {
            return uniqueColumns;
        }

        public Map<String, List<Object>> getEnumValues() {
            return enumValues;
        }

        public boolean isDetailLookup() {
            return detailLookup;
        }

        public void setDetailLookup(boolean detailLookup) {
            this.detailLookup = detailLookup;
        }
    }

    /**
     * 合成各表规则。
     *
     * @param facts   接口访问事实（可按表合并多步骤）
     * @param contexts 表上下文（唯一索引列 / 枚举建议值 / 是否详情类）
     * @return 规则 JSON 与告警
     */
    public Composed compose(List<EndpointTableFact> facts, Map<String, TableContext> contexts) {
        Composed out = new Composed();
        if (facts == null || facts.isEmpty()) {
            return out;
        }
        Map<String, TableContext> ctx = contexts == null ? Map.of() : contexts;
        for (EndpointTableFact fact : facts) {
            if (fact == null || fact.getTable() == null || fact.getTable().isBlank()) {
                continue;
            }
            TableContext c = ctx.getOrDefault(fact.getTable(), new TableContext());
            ObjectNode fields = MAPPER.createObjectNode();
            composeTable(fact, c, fields, out.warnings);
            try {
                out.fieldsByTable.put(fact.getTable(), MAPPER.writeValueAsString(fields));
            } catch (Exception e) {
                log.warn("serialize fields failed, table={}: {}", fact.getTable(), e.getMessage());
            }
        }
        return out;
    }

    private void composeTable(EndpointTableFact fact, TableContext ctx,
                              ObjectNode fields, List<String> warnings) {
        // 列 → 命中值（来自 SQL 谓词）数量，用于权重分配时区分「命中值」与「补充值」
        Map<String, Integer> hitCountByColumn = new LinkedHashMap<>();
        // 列 → 全部取值（命中值在前，补充值在后）
        Map<String, List<Object>> valuesByColumn = new LinkedHashMap<>();

        // 1. 谓词 → 枚举 / 范围
        for (Predicate p : fact.getPredicates()) {
            if (p == null || p.getColumn() == null || p.getColumn().isBlank()) {
                continue;
            }
            if (p.getOp() == Op.RANGE) {
                ObjectNode rule = fields.putObject(p.getColumn());
                rule.put("type", "range");
                if (p.getMin() != null) {
                    rule.put("min", p.getMin());
                }
                if (p.getMax() != null) {
                    rule.put("max", p.getMax());
                }
                continue;
            }
            List<Object> values = dedupe(p.getValues());
            if (values.isEmpty()) {
                continue;
            }
            Capped capped = applyValueCap(values, p.getColumn(), warnings);
            if (capped.rangeMin != null || capped.rangeMax != null) {
                ObjectNode rule = fields.putObject(p.getColumn());
                rule.put("type", "range");
                rule.put("min", capped.rangeMin);
                rule.put("max", capped.rangeMax);
                continue;
            }
            if (!capped.values.isEmpty()) {
                valuesByColumn.put(p.getColumn(), capped.values);
                hitCountByColumn.put(p.getColumn(), capped.values.size());
            }
        }

        // 2. 枚举建议值：列已有谓词值时作为补充值追加，否则单独成规则
        for (Map.Entry<String, List<Object>> e : ctx.getEnumValues().entrySet()) {
            String col = e.getKey();
            if (col == null || col.isBlank() || e.getValue() == null) {
                continue;
            }
            List<Object> suggested = dedupe(e.getValue());
            if (suggested.isEmpty()) {
                continue;
            }
            List<Object> existing = valuesByColumn.get(col);
            if (existing == null) {
                Capped capped = applyValueCap(suggested, col, warnings);
                if (capped.rangeMin != null || capped.rangeMax != null) {
                    ObjectNode rule = fields.putObject(col);
                    rule.put("type", "range");
                    rule.put("min", capped.rangeMin);
                    rule.put("max", capped.rangeMax);
                    continue;
                }
                if (!capped.values.isEmpty()) {
                    valuesByColumn.put(col, capped.values);
                    hitCountByColumn.put(col, 0);
                }
                continue;
            }
            // 补充值：只追加不在命中值中的，且总数不超上限
            for (Object v : suggested) {
                if (existing.size() >= MAX_ENUM_VALUES) {
                    break;
                }
                if (!containsObject(existing, v)) {
                    existing.add(v);
                }
            }
        }

        // 3. 外键引用边 → ref / refSkew（JOIN 关联与详情类热点倾斜）
        for (com.perftestor.datagen.model.EndpointTableFact.JoinRef ref : ctx.getJoinRefs()) {
            if (ref == null || ref.getChildColumn() == null || ref.getParentTable() == null) {
                continue;
            }
            // 该列已有更具体的谓词/范围规则时不覆盖——谓词值保证接口能查到，优先级更高
            if (valuesByColumn.containsKey(ref.getChildColumn()) || fields.has(ref.getChildColumn())) {
                continue;
            }
            ObjectNode rule = refRule(ref.getParentTable(), ref.getParentColumn(), ctx.isDetailLookup());
            fields.set(ref.getChildColumn(), rule);
        }

        // 4. 唯一索引 / 业务编码列 → seq（只落 prefix）；已有规则的列不覆盖
        for (String col : ctx.getUniqueColumns()) {
            if (col == null || col.isBlank() || valuesByColumn.containsKey(col) || fields.has(col)) {
                continue;
            }
            ObjectNode rule = fields.putObject(col);
            rule.put("type", com.perftestor.datagen.model.FieldRule.TYPE_SEQ);
            rule.put("seqPrefix", seqPrefix(col));
        }

        // 4. 统一写出枚举规则与权重（命中值占 HIT_WEIGHT_PERCENT%，补充值均分剩余）
        for (Map.Entry<String, List<Object>> e : valuesByColumn.entrySet()) {
            ObjectNode rule = fields.putObject(e.getKey());
            withWeights(rule, e.getValue(), hitCountByColumn.getOrDefault(e.getKey(), 0));
        }
    }

    private static boolean containsObject(List<Object> list, Object v) {
        for (Object o : list) {
            if (o == null ? v == null : String.valueOf(o).equals(String.valueOf(v))) {
                return true;
            }
        }
        return false;
    }

    /**
     * 值集上限处理：超限后纯数值转 range，混合类型均匀采样保留 MAX_ENUM_VALUES 个。
     *
     * @return 实际保留的值列表
     */
    /** 值集上限处理结果：二选一——保留的枚举值，或转成的范围边界 */
    private static final class Capped {
        private final List<Object> values;
        private final Double rangeMin;
        private final Double rangeMax;

        Capped(List<Object> values) {
            this.values = values;
            this.rangeMin = null;
            this.rangeMax = null;
        }

        Capped(Double rangeMin, Double rangeMax) {
            this.values = List.of();
            this.rangeMin = rangeMin;
            this.rangeMax = rangeMax;
        }
    }

    private Capped applyValueCap(List<Object> values, String column, List<String> warnings) {
        if (values.size() <= MAX_ENUM_VALUES) {
            return new Capped(values);
        }
        boolean allNumeric = values.stream().allMatch(v -> v instanceof Number);
        if (allNumeric) {
            double min = values.stream().mapToDouble(v -> ((Number) v).doubleValue()).min().orElse(0);
            double max = values.stream().mapToDouble(v -> ((Number) v).doubleValue()).max().orElse(0);
            warnings.add(String.format(Locale.ROOT,
                    "列 %s 谓词值 %d 个超过上限 %d，已改为范围规则 [%s, %s]",
                    column, values.size(), MAX_ENUM_VALUES, min, max));
            return new Capped(min, max);
        }
        List<Object> sampled = evenSample(values);
        warnings.add(String.format(Locale.ROOT,
                "列 %s 谓词值 %d 个超过上限 %d，已均匀采样保留 %d 个",
                column, values.size(), MAX_ENUM_VALUES, sampled.size()));
        return new Capped(sampled);
    }

    /** 均匀采样（保留首尾，保证区间边界不丢） */
    static List<Object> evenSample(List<Object> values) {
        List<Object> out = new ArrayList<>();
        int n = values.size();
        double step = (double) (n - 1) / (MAX_ENUM_VALUES - 1);
        int last = -1;
        for (int i = 0; i < MAX_ENUM_VALUES; i++) {
            int idx = Math.min(n - 1, (int) Math.round(i * step));
            if (idx != last) {
                out.add(values.get(idx));
                last = idx;
            }
        }
        return out;
    }

    /**
     * 权重：前 hitCount 个是命中值（来自 SQL 谓词），共占 HIT_WEIGHT_PERCENT%；
     * 其余为补充值，均分剩余。保证接口查得到命中数据，同时保有一定区分度。
     */
    private void withWeights(ObjectNode rule, List<Object> values, int hitCount) {
        rule.put("type", "enum");
        ArrayNode arr = rule.putArray("values");
        values.forEach(v -> putValue(arr, v));
        ArrayNode weights = rule.putArray("weights");
        int n = values.size();
        if (n == 0) {
            return;
        }
        int hit = Math.max(0, Math.min(hitCount, n));
        int rest = n - hit;
        double perHit;
        double perRest;
        if (rest > 0 && hit > 0) {
            perHit = (double) HIT_WEIGHT_PERCENT / hit;
            perRest = (double) (100 - HIT_WEIGHT_PERCENT) / rest;
        } else {
            // 无补充值（或全是补充值）时归一化到 100，保证权重和为 100%
            perHit = 100.0 / n;
            perRest = perHit;
        }
        for (int i = 0; i < n; i++) {
            weights.add(i < hit ? perHit : perRest);
        }
    }

    /** 关联引用规则：JOIN 父表 / 详情类按 id 查询 */
    public ObjectNode refRule(String parentTable, String parentColumn, boolean detail) {
        ObjectNode rule = com.fasterxml.jackson.databind.node.JsonNodeFactory.instance.objectNode();
        rule.put("type", detail ? "refSkew" : "ref");
        rule.put("table", parentTable);
        rule.put("column", parentColumn);
        if (detail) {
            rule.put("hotPercent", HOT_PERCENT);
            rule.put("hotShare", HOT_SHARE);
        }
        return rule;
    }

    private static void putValue(ArrayNode arr, Object v) {
        if (v instanceof Number n) {
            arr.add(n.doubleValue());
        } else if (v instanceof Boolean b) {
            arr.add(b);
        } else {
            arr.add(String.valueOf(v));
        }
    }

    private static List<Object> dedupe(Collection<Object> values) {
        Set<String> seen = new LinkedHashSet<>();
        List<Object> out = new ArrayList<>();
        for (Object v : values) {
            if (v == null) {
                continue;
            }
            if (seen.add(String.valueOf(v))) {
                out.add(v);
            }
        }
        return out;
    }

    /**
     * seq 前缀：取列名下划线分段的首字母大写拼接。
     * 例：order_no → ORD_、couponCode → CC_。列名无法解析时回退表名前缀。
     */
    static String seqPrefix(String column) {
        if (column == null || column.isBlank()) {
            return "S_";
        }
        StringBuilder sb = new StringBuilder();
        for (String part : column.split("[^A-Za-z0-9]+")) {
            if (!part.isEmpty()) {
                sb.append(Character.toUpperCase(part.charAt(0)));
            }
        }
        if (sb.length() == 0) {
            return "S_";
        }
        return sb.length() > 6 ? sb.substring(0, 6) + "_" : sb + "_";
    }
}
