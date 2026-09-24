package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.perfserver.ai.AiKnowledgeService;
import com.perfserver.entity.AiKnowledge;
import org.springframework.stereotype.Component;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 知识库检索工具：全文检索历史性能结论与修复经验（MySQL FULLTEXT ngram）。
 *
 * <p>用于让模型在给出根因/修复建议前先参考平台沉淀过的同类结论，
 * 避免重复分析且保持结论口径一致。
 */
@Component
public class SearchKnowledgeTool extends AbstractAiTool {

    private final AiKnowledgeService knowledgeService;

    public SearchKnowledgeTool(AiKnowledgeService knowledgeService) {
        this.knowledgeService = knowledgeService;
    }

    @Override
    public String name() {
        return "search_knowledge";
    }

    @Override
    public String description() {
        return "检索性能知识库（历史分析结论与修复经验）。参数：query 检索关键词（中文即可，必填）；"
                + "app 应用名（可选，空表示全部；传 app 时本应用结论与全局结论一并召回，"
                + "返回项 appName 为空表示跨项目共性经验）；limit 条数上限（默认 5，最大 20）。"
                + "分析根因或给出修复建议前应优先检索，复用已有结论。"
                + "注意：返回 count=0 表示知识库确实没有匹配的历史结论，"
                + "不代表当前没有问题，应按证据链独立分析。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"query", "app", "limit"}, new String[]{"query"}).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        String query = str(args, "query", "");
        String app = str(args, "app", null);
        int limit = integer(args, "limit", 5);
        try {
            List<AiKnowledge> hits = knowledgeService.search(query, app, limit);
            ArrayNode list = mapper.createArrayNode();
            for (AiKnowledge k : hits) {
                var n = list.addObject();
                n.put("id", k.getId());
                n.put("appName", k.getAppName());
                n.put("title", k.getTitle());
                n.put("content", AiKnowledgeService.snippet(k.getContent()));
                n.put("tags", k.getTags());
                n.put("score", k.getScore() == null ? 0 : k.getScore());
                n.put("createTime", k.getCreateTime() == null ? "" : k.getCreateTime().toString());
            }
            Map<String, Object> data = new LinkedHashMap<>();
            data.put("query", query);
            data.put("count", hits.size());
            data.put("items", list);
            return ok(data);
        } catch (Exception e) {
            return error("知识库检索失败: " + e.getMessage());
        }
    }
}
