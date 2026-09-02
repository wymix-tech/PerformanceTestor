package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perftestor.datagen.model.FieldRule;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;

/**
 * seq 起始值刷新器：执行时探测目标列 {@code MAX(col)+1} 并写入规则。
 *
 * <p>存在的理由：数据方案要支持<b>重复执行</b>。若在 analyze 阶段固化
 * {@code seqStart = 已有 max + 1}，第二次执行必然撞唯一键。故方案里只存
 * {@code seqPrefix}，每次执行前重新探测。</p>
 *
 * <p>探测方式：目标列是数值类型时取 {@code MAX(col)}；字符串类型时取
 * {@code MAX(CAST(col AS ...))} 不可用，退化为 {@code SELECT MAX(col)} 的自然序，
 * 再尝试剥离前缀后转数值。任一环节失败回退 1 并写告警，不阻断执行。</p>
 */
@Component
public class SeqStartResolver {

    private static final Logger log = LoggerFactory.getLogger(SeqStartResolver.class);

    /** 探测失败时的回退起始值 */
    public static final long FALLBACK_START = 1L;

    private final ObjectMapper objectMapper;

    public SeqStartResolver(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    /** 刷新结果 */
    public static final class RefreshResult {
        private final String fieldsJson;
        private final List<String> warnings = new ArrayList<>();

        RefreshResult(String fieldsJson) {
            this.fieldsJson = fieldsJson;
        }

        RefreshResult(String fieldsJson, List<String> warnings) {
            this.fieldsJson = fieldsJson;
            if (warnings != null) {
                this.warnings.addAll(warnings);
            }
        }

        public String getFieldsJson() {
            return fieldsJson;
        }

        public List<String> getWarnings() {
            return warnings;
        }
    }

    /**
     * 刷新字段规则 JSON 中所有 seq 规则的 seqStart。
     *
     * @param conn       目标库连接（调用方负责关闭）
     * @param table      表名
     * @param fieldsJson 字段规则 JSON（{"col":{"type":"seq","seqPrefix":"ORD_"}}）
     * @return 刷新后的 JSON（无 seq 规则时原样返回）
     */
    public RefreshResult refresh(Connection conn, String table, String fieldsJson) {
        RefreshResult out = new RefreshResult(fieldsJson);
        if (fieldsJson == null || fieldsJson.isBlank() || conn == null) {
            return out;
        }
        try {
            JsonNode root = objectMapper.readTree(fieldsJson);
            if (root == null || !root.isObject()) {
                return out;
            }
            boolean changed = false;
            Iterator<String> it = root.fieldNames();
            while (it.hasNext()) {
                String col = it.next();
                JsonNode rule = root.get(col);
                if (rule == null || !FieldRule.TYPE_SEQ.equalsIgnoreCase(rule.path("type").asText(""))) {
                    continue;
                }
                String prefix = rule.path("seqPrefix").asText("");
                long start = detectStart(conn, table, col, prefix);
                if (start <= 0) {
                    start = FALLBACK_START;
                    out.warnings.add(String.format(Locale.ROOT,
                            "列 %s.%s 序号起始值探测失败，已回退为 %d（若库内已有数据可能撞唯一键）",
                            table, col, FALLBACK_START));
                }
                ((ObjectNode) root).putObject(col).put("type", FieldRule.TYPE_SEQ)
                        .put("seqPrefix", prefix).put("seqStart", start);
                changed = true;
            }
            if (changed) {
                // 保留循环内累积的告警：重赋值会丢弃 warnings
                out = new RefreshResult(objectMapper.writeValueAsString(root), out.getWarnings());
            }
        } catch (Exception e) {
            log.warn("refresh seqStart failed, table={}: {}", table, e.getMessage());
            out.warnings.add("序号起始值刷新失败: " + e.getMessage());
        }
        return new RefreshResult(out.getFieldsJson(), out.getWarnings());
    }

    /**
     * 探测起始值：先按纯数值取 MAX，失败再按「剥离前缀后转数值」取 MAX。
     * 返回 0 表示探测失败（调用方回退）。
     */
    long detectStart(Connection conn, String table, String column, String prefix) {
        long numeric = queryMax(conn, "SELECT MAX(" + quote(column) + ") FROM " + quote(table));
        if (numeric > 0) {
            return numeric + 1;
        }
        if (prefix == null || prefix.isBlank()) {
            return 0;
        }
        // 字符串型业务编码：剥离前缀后取数值最大
        String sql = "SELECT MAX(CAST(SUBSTRING(" + quote(column) + ", "
                + (prefix.length() + 1) + ") AS DECIMAL(38,0))) FROM " + quote(table);
        long stripped = queryMax(conn, sql);
        return stripped > 0 ? stripped + 1 : 0;
    }

    private long queryMax(Connection conn, String sql) {
        try (PreparedStatement ps = conn.prepareStatement(sql); ResultSet rs = ps.executeQuery()) {
            if (rs.next()) {
                Object v = rs.getObject(1);
                if (v instanceof Number n) {
                    return n.longValue();
                }
                if (v != null) {
                    String s = String.valueOf(v);
                    if (s.matches("\\d+")) {
                        return Long.parseLong(s);
                    }
                }
            }
        } catch (Exception e) {
            log.debug("query max failed: {} -> {}", sql, e.getMessage());
        }
        return 0;
    }

    private static String quote(String ident) {
        return "`" + ident.replace("`", "``") + "`";
    }
}
