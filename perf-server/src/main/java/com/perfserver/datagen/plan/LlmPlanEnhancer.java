package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.ai.LlmClient;
import com.perftestor.datagen.model.EndpointTableFact;
import com.perftestor.datagen.model.EndpointTableFact.Predicate;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * LLM 方案增强器：静态分析结论为主，大模型仅做语义补全与兜底。
 *
 * <p><b>脱敏边界（硬性要求，由 {@code LlmPlanEnhancerSanitizeTest} 守卫）</b>：</p>
 * <ul>
 *   <li>允许发送：表名、列名、列类型、读写类型、谓词算子形状（{@code =} / {@code IN(n=3)} / {@code BETWEEN}）、
 *       {@code unresolved} 原因、索引类型（唯一/普通）</li>
 *   <li>值一律打码：字面值替换为类型占位（{@code <int>} / {@code <str:len=8>} / {@code <date>}）</li>
 *   <li>禁止发送：SQL 全文、源码片段、连接串与凭据、真实数据样本、文件路径以外的仓库信息</li>
 * </ul>
 *
 * <p>降级：无 API Key（{@link LlmClient#available()} 为 false）、调用返回 null、
 * 解析失败时<b>静默降级</b>为纯静态结论，并把原因写入方案 warnings，绝不阻断分析。</p>
 */
@Component
public class LlmPlanEnhancer {

    private static final Logger log = LoggerFactory.getLogger(LlmPlanEnhancer.class);

    /** 送审的最大表数（控制成本与提示词长度） */
    public static final int MAX_TABLES = 20;

    private static final String SYSTEM_PROMPT = "你是一个测试数据规划助手。根据给定的数据库结构元信息，"
            + "补充数据准备建议。只输出 JSON，不要解释。所有值均已脱敏为类型占位符，不要尝试猜测真实业务值。";

    private final LlmClient llmClient;
    private final ObjectMapper objectMapper;

    public LlmPlanEnhancer(LlmClient llmClient, ObjectMapper objectMapper) {
        this.llmClient = llmClient;
        this.objectMapper = objectMapper;
    }

    /** 增强结果 */
    public static final class Enhancement {
        private final boolean applied;
        private final String reason;
        private final List<String> warnings = new ArrayList<>();

        Enhancement(boolean applied, String reason) {
            this.applied = applied;
            this.reason = reason;
        }

        public boolean isApplied() {
            return applied;
        }

        public String getReason() {
            return reason;
        }

        public List<String> getWarnings() {
            return warnings;
        }
    }

    /**
     * 尝试用 LLM 补全静态分析未覆盖的部分。
     *
     * @param facts    静态分析事实（含 unresolved 原因）
     * @param enabled  用户是否显式开启 LLM 增强
     * @return 增强结果（未开启/无 Key/失败时 applied=false 并给出原因）
     */
    public Enhancement enhance(List<EndpointTableFact> facts, boolean enabled) {
        if (!enabled) {
            return new Enhancement(false, "未开启 LLM 增强");
        }
        if (facts == null || facts.isEmpty()) {
            return new Enhancement(false, "无静态分析结论，跳过 LLM 增强");
        }
        if (llmClient == null || !llmClient.available()) {
            return new Enhancement(false, "未配置大模型 API Key，已降级为纯静态分析结论");
        }
        try {
            String prompt = sanitizedPrompt(facts);
            String raw = llmClient.chatJson(SYSTEM_PROMPT, prompt, null);
            if (raw == null || raw.isBlank()) {
                return new Enhancement(false, "大模型无有效返回，已降级为纯静态分析结论");
            }
            JsonNode parsed = objectMapper.readTree(raw);
            if (parsed == null || parsed.isMissingNode()) {
                return new Enhancement(false, "大模型返回无法解析，已降级为纯静态分析结论");
            }
            int merged = mergeEnumSuggestions(facts, parsed);
            if (merged == 0) {
                // 解析成功但没采纳任何建议时不能报 applied=true——
                // 那会让前端展示「增强已应用」的假象，而实际什么都没变
                return new Enhancement(false, "大模型未给出可采纳的补全建议，已沿用纯静态分析结论");
            }
            Enhancement out = new Enhancement(true, "LLM 增强已应用（补充 " + merged + " 列的取值建议）");
            out.warnings.add("本方案含大模型语义补全，请复核表清单后再执行");
            return out;
        } catch (Exception e) {
            log.warn("llm plan enhance failed: {}", e.getMessage());
            return new Enhancement(false, "大模型调用失败，已降级为纯静态分析结论: " + e.getMessage());
        }
    }

    /**
     * 把 LLM 建议合入事实，返回实际采纳的列数。
     *
     * <p><b>只合入「已有列的枚举取值补充」</b>，不接受新表、新列、新关联。
     * 理由：本项目对表识别坚持「宁缺勿滥」——对错误的表造数据会污染业务表，
     * 危害远大于漏表。让模型凭语义猜测表名/关联，等于把这条底线交出去。
     * 补充取值则不然：列已由静态分析确认存在，取值只影响数据分布，风险可控。</p>
     *
     * <p>期望结构：{@code {"tables":[{"table":"t","columns":[{"column":"c","values":[...]}]}]}}</p>
     */
    int mergeEnumSuggestions(List<EndpointTableFact> facts, JsonNode parsed) {
        JsonNode tables = parsed.isArray() ? parsed : parsed.get("tables");
        if (tables == null || !tables.isArray()) {
            return 0;
        }
        Map<String, EndpointTableFact> byTable = new java.util.HashMap<>();
        for (EndpointTableFact f : facts) {
            if (f != null && f.getTable() != null) {
                byTable.put(f.getTable().toLowerCase(java.util.Locale.ROOT), f);
            }
        }
        int merged = 0;
        for (JsonNode t : tables) {
            String tableName = t.path("table").asText("");
            EndpointTableFact fact = byTable.get(tableName.trim().toLowerCase(java.util.Locale.ROOT));
            if (fact == null) {
                log.debug("llm suggested unknown table, ignored: {}", tableName);
                continue;
            }
            JsonNode columns = t.get("columns");
            if (columns == null || !columns.isArray()) {
                continue;
            }
            for (JsonNode c : columns) {
                String col = c.path("column").asText("");
                JsonNode valuesNode = c.get("values");
                if (col.isBlank() || valuesNode == null || !valuesNode.isArray() || valuesNode.isEmpty()) {
                    continue;
                }
                // 只补充已存在谓词的列（列的存在性由静态分析保证）
                EndpointTableFact.Predicate target = null;
                for (EndpointTableFact.Predicate p : fact.getPredicates()) {
                    if (col.equalsIgnoreCase(p.getColumn())
                            && (p.getOp() == EndpointTableFact.Op.EQ || p.getOp() == EndpointTableFact.Op.IN)) {
                        target = p;
                        break;
                    }
                }
                if (target == null) {
                    continue;
                }
                int before = target.getValues().size();
                for (JsonNode v : valuesNode) {
                    if (target.getValues().size() >= MAX_MERGED_VALUES) {
                        break;
                    }
                    if (v.isTextual()) {
                        addIfAbsent(target.getValues(), v.asText());
                    } else if (v.isNumber()) {
                        addIfAbsent(target.getValues(), v.asLong());
                    }
                }
                if (target.getValues().size() > before) {
                    merged++;
                }
            }
        }
        return merged;
    }

    /** 单次合入的取值上限（防止模型返回超长列表撑爆值集） */
    public static final int MAX_MERGED_VALUES = 32;

    private static void addIfAbsent(List<Object> values, Object v) {
        for (Object o : values) {
            if (o != null && String.valueOf(o).equals(String.valueOf(v))) {
                return;
            }
        }
        values.add(v);
    }

    /**
     * 构造脱敏后的提示词：只发结构元信息，所有字面值替换为类型占位。
     */
    String sanitizedPrompt(List<EndpointTableFact> facts) {
        ObjectNode root = objectMapper.createObjectNode();
        ArrayNode tables = root.putArray("tables");
        List<EndpointTableFact> limited = facts.size() > MAX_TABLES ? facts.subList(0, MAX_TABLES) : facts;
        for (EndpointTableFact f : limited) {
            ObjectNode t = tables.addObject();
            t.put("table", f.getTable());
            t.put("access", f.getAccess() == null ? "READ" : f.getAccess().name());
            t.put("confidence", f.getConfidence());
            if (f.getUnresolvedReason() != null && !f.getUnresolvedReason().isBlank()) {
                // unresolvedReason 可能来自 SQL 片段，必须先脱敏再外发（禁止泄漏 SQL 全文与字面值）
                t.put("unresolvedReason", redact(f.getUnresolvedReason()));
            }
            ArrayNode preds = t.putArray("predicates");
            for (Predicate p : f.getPredicates()) {
                ObjectNode pn = preds.addObject();
                pn.put("column", p.getColumn());
                pn.put("op", p.getOp() == null ? "" : p.getOp().name());
                pn.put("valueCount", p.getValues() == null ? 0 : p.getValues().size());
                ArrayNode shapes = pn.putArray("valueShapes");
                if (p.getValues() != null) {
                    p.getValues().stream().limit(3).forEach(v -> shapes.add(shapeOf(v)));
                }
                if (p.getOp() == EndpointTableFact.Op.RANGE) {
                    pn.put("hasBounds", p.getMin() != null || p.getMax() != null);
                }
            }
            ArrayNode joins = t.putArray("joinTables");
            if (f.getJoinTables() != null) {
                f.getJoinTables().forEach(joins::add);
            }
        }
        return root.toString();
    }

    /**
     * 自由文本脱敏：剥离单引号字符串、数字串与超长内容。
     *
     * <p>{@code unresolvedReason} 可能来自 SQL 片段（含表名、字面值与连接信息），
     * 直接外发会突破脱敏边界，故统一走本方法。</p>
     */
    static String redact(String text) {
        String s = text.replaceAll("'[^']*'", "'<redacted>'")
                .replaceAll("\\d{3,}", "<num>")
                .replaceAll("(?i)\\b(jdbc:[a-z]+://[^\\s]+)", "<conn>");
        return s.length() > 200 ? s.substring(0, 200) + "..." : s;
    }

    /** 值的类型占位：只暴露类型与长度，不暴露内容 */
    static String shapeOf(Object v) {
        if (v == null) {
            return "<null>";
        }
        if (v instanceof Number n) {
            return n.toString().contains(".") ? "<decimal>" : "<int>";
        }
        String s = String.valueOf(v);
        if (s.matches("\\d{4}-\\d{2}-\\d{2}.*")) {
            return "<date>";
        }
        if (s.matches("-?\\d+")) {
            return "<int>";
        }
        if (s.matches("-?\\d+\\.\\d+")) {
            return "<decimal>";
        }
        return "<str:len=" + s.length() + ">";
    }

    /** 断言用：提示词中是否残留原始字面值（测试守卫） */
    static boolean containsLiteral(String prompt, String literal) {
        return prompt != null && literal != null && !literal.isBlank()
                && prompt.toLowerCase(Locale.ROOT).contains(literal.toLowerCase(Locale.ROOT));
    }
}
