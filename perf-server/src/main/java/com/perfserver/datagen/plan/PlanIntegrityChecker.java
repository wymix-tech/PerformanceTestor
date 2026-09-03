package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * 方案完整性预检器：在<b>构建 DataGenSpec 之前</b>补齐引用到的父表，并处理环依赖降级。
 *
 * <p>为何必须早于构建 spec：引擎 {@code ExplicitRuleHints} 的过滤依据是 {@code metaMap}，
 * 而 metaMap 由 {@code spec.getTables()} 的 DB 元数据构建。父表若不在 tables 清单内，
 * explicit 边会被静默丢弃——ref 池为空后生成逻辑退化为随机数，数据毫无关联，
 * 而<b>全流程无任何报错</b>。这是本次评审发现的最危险缺口，静默失效比报错难查得多。</p>
 *
 * <p>环依赖：引擎拓扑排序会抛 {@code IllegalArgumentException}（消息已定位环中表名）。
 * 本类提供降级策略——剔除环中置信度最低的一条 ref 边后重试一次；仍失败则写告警，
 * 交用户手工调整，不让任务直接失败。</p>
 */
@Component
public class PlanIntegrityChecker {

    private static final Logger log = LoggerFactory.getLogger(PlanIntegrityChecker.class);

    /** 环依赖降级重试上限（硬编码 1 次，避免无限重试） */
    public static final int MAX_CYCLE_RETRY = 1;

    /** 规则 JSON 未带 confidence 时的边置信度（等同「无证据的默认关联」） */
    public static final double DEFAULT_EDGE_CONFIDENCE = 1.0;

    private final ObjectMapper objectMapper;

    public PlanIntegrityChecker(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    /** 方案中的一张表 */
    public static final class PlanTable {
        private String table;
        private int rowCount;
        private String fields;
        private String access;
        private List<String> evidence = new ArrayList<>();

        public PlanTable() {
        }

        public PlanTable(String table, int rowCount, String fields, String access) {
            this.table = table;
            this.rowCount = rowCount;
            this.fields = fields;
            this.access = access;
        }

        public String getTable() {
            return table;
        }

        public void setTable(String table) {
            this.table = table;
        }

        public int getRowCount() {
            return rowCount;
        }

        public void setRowCount(int rowCount) {
            this.rowCount = rowCount;
        }

        public String getFields() {
            return fields;
        }

        public void setFields(String fields) {
            this.fields = fields;
        }

        public String getAccess() {
            return access;
        }

        public void setAccess(String access) {
            this.access = access;
        }

        public List<String> getEvidence() {
            return evidence;
        }

        public void setEvidence(List<String> evidence) {
            this.evidence = evidence;
        }
    }

    /** 预检结果 */
    public static final class CheckResult {
        private final List<PlanTable> tables;
        private final List<String> warnings = new ArrayList<>();

        CheckResult(List<PlanTable> tables) {
            this.tables = tables;
        }

        public List<PlanTable> getTables() {
            return tables;
        }

        public List<String> getWarnings() {
            return warnings;
        }
    }

    /**
     * 预检：把所有 ref/refSkew 引用到的父表并入表清单。
     *
     * @param tables 方案表清单
     * @return 补齐后的清单与告警（含新增父表的原因说明）
     */
    public CheckResult completeParents(List<PlanTable> tables) {
        // 拷贝为可变列表：入参可能是 List.of(...) 等不可变集合，补父表需要写入
        CheckResult out = new CheckResult(tables == null
                ? new ArrayList<>() : new ArrayList<>(tables));
        if (out.tables.isEmpty()) {
            return out;
        }
        Set<String> present = new LinkedHashSet<>();
        for (PlanTable t : out.tables) {
            if (t.getTable() != null) {
                present.add(t.getTable().toLowerCase(Locale.ROOT));
            }
        }

        List<PlanTable> added = new ArrayList<>();
        for (PlanTable t : new ArrayList<>(out.tables)) {
            List<String> refs = refTargets(t.getFields());
            for (String parent : refs) {
                if (present.contains(parent)) {
                    continue;
                }
                present.add(parent);
                PlanTable p = new PlanTable(parent, RowCountEstimator.parentRows(t.getRowCount()),
                        null, "READ");
                p.getEvidence().add("由表 " + t.getTable() + " 的外键引用自动补入（保证 ref 池非空）");
                added.add(p);
                out.warnings.add(String.format(Locale.ROOT,
                        "父表 %s 不在方案中，已自动补入（%d 行）——缺失会导致子表外键值退化为随机数且无任何报错",
                        parent, p.getRowCount()));
            }
        }
        out.tables.addAll(added);
        return out;
    }

    /** 提取字段规则中所有 ref/refSkew 的目标表名 */
    List<String> refTargets(String fieldsJson) {
        List<String> out = new ArrayList<>();
        if (fieldsJson == null || fieldsJson.isBlank()) {
            return out;
        }
        try {
            JsonNode root = objectMapper.readTree(fieldsJson);
            if (root == null || !root.isObject()) {
                return out;
            }
            Iterator<String> it = root.fieldNames();
            while (it.hasNext()) {
                JsonNode rule = root.get(it.next());
                if (rule == null || !rule.isObject()) {
                    continue;
                }
                String type = rule.path("type").asText("");
                if (!"ref".equalsIgnoreCase(type) && !"refSkew".equalsIgnoreCase(type)) {
                    continue;
                }
                String parent = rule.path("table").asText("");
                if (!parent.isBlank()) {
                    out.add(parent.toLowerCase(Locale.ROOT));
                }
            }
        } catch (Exception e) {
            log.debug("parse ref targets failed: {}", e.getMessage());
        }
        return out;
    }

    /**
     * 环依赖降级：剔除环中置信度最低的一条 ref 边。
     *
     * @param tables       表清单
     * @param cycleMessage 引擎抛出的环信息（形如 "a -&gt; b -&gt; a"）
     * @param edgeConfidence 边 → 置信度（由调用方按事实填充）
     * @return true 表示已剔除一条边，调用方可重试一次
     */
    public boolean breakWeakestCycleEdge(List<PlanTable> tables, String cycleMessage,
                                         java.util.Map<String, Double> edgeConfidence) {
        if (tables == null || tables.isEmpty() || cycleMessage == null) {
            return false;
        }
        List<String> cycleNodes = parseCycle(cycleMessage);
        if (cycleNodes.size() < 2) {
            return false;
        }
        String victim = null;
        double lowest = Double.MAX_VALUE;
        for (int i = 0; i < cycleNodes.size(); i++) {
            String child = cycleNodes.get(i);
            String parent = cycleNodes.get((i + 1) % cycleNodes.size());
            String key = child + "->" + parent;
            double conf = edgeConfidence == null ? DEFAULT_EDGE_CONFIDENCE
                    : edgeConfidence.getOrDefault(key, DEFAULT_EDGE_CONFIDENCE);
            if (conf < lowest) {
                lowest = conf;
                victim = key;
            }
        }
        if (victim == null) {
            return false;
        }
        int sep = victim.indexOf("->");
        return removeRefEdge(tables, victim.substring(0, sep), victim.substring(sep + 2));
    }

    /**
     * 从表清单的 ref/refSkew 规则中提取「子表 -&gt; 父表」边置信度。
     *
     * <p>存在的理由：环依赖降级要剔除<b>置信度最低</b>的那条边，而置信度只可能来自规则 JSON
     * （它由 {@link PlanRuleComposer} 按外键证据强度写入）。此前调用方直接传 {@code Map.of()}，
     * 所有边都落到默认值，「最低置信度」退化成「环中第一条边」——行为与告警文案不符，
     * 且被剔除的很可能是 JOIN ON 这种高置信边，而低置信的命名猜测边被保留了。</p>
     *
     * @param tables 表清单
     * @return 边 key（{@code 子表->父表}，全小写）→ 置信度
     */
    public Map<String, Double> edgeConfidence(List<PlanTable> tables) {
        Map<String, Double> out = new java.util.HashMap<>();
        if (tables == null) {
            return out;
        }
        for (PlanTable t : tables) {
            String child = t.getTable() == null ? null : t.getTable().toLowerCase(Locale.ROOT);
            if (child == null || child.isBlank() || t.getFields() == null || t.getFields().isBlank()) {
                continue;
            }
            try {
                JsonNode root = objectMapper.readTree(t.getFields());
                if (root == null || !root.isObject()) {
                    continue;
                }
                Iterator<String> it = root.fieldNames();
                while (it.hasNext()) {
                    JsonNode rule = root.get(it.next());
                    if (rule == null || !rule.isObject()) {
                        continue;
                    }
                    String type = rule.path("type").asText("");
                    if (!"ref".equalsIgnoreCase(type) && !"refSkew".equalsIgnoreCase(type)) {
                        continue;
                    }
                    String parent = rule.path("table").asText("").toLowerCase(Locale.ROOT);
                    if (parent.isBlank()) {
                        continue;
                    }
                    double conf = rule.hasNonNull("confidence")
                            ? rule.path("confidence").asDouble(DEFAULT_EDGE_CONFIDENCE)
                            : DEFAULT_EDGE_CONFIDENCE;
                    // 同一对表存在多条 ref 列时取最高置信度：removeRefEdge 会一次性移除该
                    // 方向上的所有 ref 列，按最强的那条衡量这条边是否可信
                    out.merge(child + "->" + parent, conf, Double::max);
                }
            } catch (Exception e) {
                log.debug("extract edge confidence failed, table={}: {}", child, e.getMessage());
            }
        }
        return out;
    }

    /** 从环消息中解析节点：支持 "a -> b -> a" 与 "a->b->a" */
    List<String> parseCycle(String message) {
        List<String> nodes = new ArrayList<>();
        for (String part : message.split("\\s*->\\s*")) {
            String n = part.trim().replaceAll("[^A-Za-z0-9_]", "");
            if (!n.isEmpty()) {
                nodes.add(n.toLowerCase(Locale.ROOT));
            }
        }
        // 去掉消息前缀（如 "表组存在循环依赖，无法确定生成顺序: "）
        if (nodes.size() > 2 && nodes.get(0).equals(nodes.get(nodes.size() - 1))) {
            nodes.remove(nodes.size() - 1);
        }
        return nodes;
    }

    /** 移除子表中指向指定父表的 ref/refSkew 规则 */
    boolean removeRefEdge(List<PlanTable> tables, String child, String parent) {
        boolean removed = false;
        for (PlanTable t : tables) {
            if (t.getTable() == null || !t.getTable().equalsIgnoreCase(child)
                    || t.getFields() == null || t.getFields().isBlank()) {
                continue;
            }
            try {
                JsonNode root = objectMapper.readTree(t.getFields());
                if (root == null || !root.isObject()) {
                    continue;
                }
                ObjectNode copy = (ObjectNode) root;
                Iterator<String> it = copy.fieldNames();
                List<String> toRemove = new ArrayList<>();
                while (it.hasNext()) {
                    String col = it.next();
                    JsonNode rule = copy.get(col);
                    if (rule == null || !rule.isObject()) {
                        continue;
                    }
                    String type = rule.path("type").asText("");
                    if (("ref".equalsIgnoreCase(type) || "refSkew".equalsIgnoreCase(type))
                            && parent.equalsIgnoreCase(rule.path("table").asText(""))) {
                        toRemove.add(col);
                    }
                }
                if (!toRemove.isEmpty()) {
                    toRemove.forEach(copy::remove);
                    t.setFields(objectMapper.writeValueAsString(copy));
                    removed = true;
                    log.warn("cycle detected: removed ref edge {} -> {} (column {})", child, parent, toRemove);
                }
            } catch (Exception e) {
                log.debug("remove ref edge failed, child={}: {}", child, e.getMessage());
            }
        }
        return removed;
    }
}
