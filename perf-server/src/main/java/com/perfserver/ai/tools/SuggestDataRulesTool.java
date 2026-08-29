package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.entity.SemanticHint;
import com.perfserver.mapper.SemanticHintMapper;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * suggest_data_rules AI 工具：综合表结构 + 语义提示库（魔法值候选/关联建议）产出
 * 完整 ruleJson 建议（值集/权重/范围推断）。规则档（无 LLM）直接由语义提示合成；
 * LLM 档下模型拿到本工具结果后二次加工；语义提示为空时返回列结构兜底建议。
 */
@Component
public class SuggestDataRulesTool extends AbstractAiTool {

    private final SemanticHintMapper hintMapper;

    public SuggestDataRulesTool(SemanticHintMapper hintMapper) {
        this.hintMapper = hintMapper;
    }

    @Override
    public String name() {
        return "suggest_data_rules";
    }

    @Override
    public String description() {
        return "为目标表生成测试数据规则建议（ruleJson）。参数：repoId 源码仓库 ID（可选，"
                + "提供时结合源码魔法值扫描结果）；tableName 目标表名。"
                + "返回字段建议（enum 值集与权重/范围 overThreshold/ref 关联），可直接用于灌数据任务。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"repoId", "tableName"}, new String[]{"tableName"}).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        String table = str(args, "tableName", "");
        if (table.isBlank()) {
            return error("tableName 必填");
        }
        Long repoId = args.hasNonNull("repoId") ? args.get("repoId").asLong() : null;
        try {
            List<SemanticHint> hints = hintMapper.query(repoId, table.toLowerCase(), null, null, 100);
            Map<String, ObjectNode> fieldRules = new LinkedHashMap<>();
            for (SemanticHint h : hints) {
                switch (h.getHintType()) {
                    case SemanticHint.TYPE_VALUE -> {
                        ObjectNode rule = fieldRules.computeIfAbsent(h.getColumnName(),
                                k -> mapper.createObjectNode());
                        rule.put("type", "enum");
                        rule.set("values", parseValues(h.getValuesJson()));
                        rule.put("_source", h.getSource());
                        rule.put("_confidence", h.getConfidence() == null ? 0.5 : h.getConfidence());
                        rule.put("suggestedWeights", true);
                    }
                    case SemanticHint.TYPE_RELATION -> {
                        ObjectNode rule = fieldRules.computeIfAbsent(h.getColumnName(),
                                k -> mapper.createObjectNode());
                        rule.put("type", h.getConfidence() != null && h.getConfidence() >= 0.8
                                ? "ref" : "refSkew");
                        rule.put("table", h.getParentTable());
                        rule.put("column", h.getParentColumn() == null ? "id" : h.getParentColumn());
                        if ("refSkew".equals(rule.path("type").asText())) {
                            rule.put("hotPercent", 20);
                            rule.put("hotShare", 80);
                        }
                        rule.put("_source", h.getSource());
                        rule.put("_confidence", h.getConfidence() == null ? 0.5 : h.getConfidence());
                    }
                    default -> {
                        // 未知类型忽略
                    }
                }
            }
            ObjectNode fields = mapper.createObjectNode();
            fieldRules.forEach(fields::set);
            ObjectNode root = mapper.createObjectNode();
            root.set("fields", fields);
            root.put("tableName", table);
            root.put("hintCount", hints.size());
            if (hints.isEmpty()) {
                root.put("note", "无语义提示（未扫描源码或表中无魔法值/关联信号），建议先执行一次带 repoId 的灌数据任务或试运行");
            }
            return ok(root);
        } catch (Exception e) {
            return error("规则建议生成失败: " + e.getMessage());
        }
    }

    private ArrayNode parseValues(String valuesJson) {
        ArrayNode arr = mapper.createArrayNode();
        try {
            JsonNode node = mapper.readTree(valuesJson == null ? "[]" : valuesJson);
            if (node.isArray()) {
                node.forEach(arr::add);
            }
        } catch (Exception ignore) {
            // 容错：空数组
        }
        return arr;
    }
}

