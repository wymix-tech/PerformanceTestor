package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.perfserver.service.SourceIssueService;
import org.springframework.stereotype.Component;

import java.util.Map;

/**
 * 修复提示词生成工具：为已选定的修复方案生成结构化提示词（Markdown），供 AI 辅助编码工具直接修复。
 */
@Component
public class GenerateFixPromptTool extends AbstractAiTool {

    private final SourceIssueService sourceIssueService;

    public GenerateFixPromptTool(SourceIssueService sourceIssueService) {
        this.sourceIssueService = sourceIssueService;
    }

    @Override
    public String name() {
        return "generate_fix_prompt";
    }

    @Override
    public String description() {
        return "为已选定的修复方案生成结构化修复提示词。参数：issueId 必填（性能问题 ID）；suggestionId 必填（方案 ID）。"
                + "返回 Markdown 提示词，包含目标文件、检测证据、根因、建议修改与验证方式，可直接复制给 AI 编码工具执行修复。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"issueId", "suggestionId"}, new String[]{"issueId", "suggestionId"}).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        long issueId = lng(args, "issueId", 0L);
        long suggestionId = lng(args, "suggestionId", 0L);
        if (issueId <= 0 || suggestionId <= 0) {
            return error("issueId 与 suggestionId 必填");
        }
        try {
            String promptMd = sourceIssueService.selectSuggestion(issueId, suggestionId);
            if (promptMd == null || promptMd.isBlank()) {
                return error("方案不存在或提示词生成失败，请先通过 analyze_code_issues 生成方案");
            }
            return ok(Map.of("issueId", issueId, "suggestionId", suggestionId,
                    "prompt_md", promptMd, "hint", "可将 prompt_md 内容直接提供给 AI 辅助编码工具执行修复"));
        } catch (Exception e) {
            return error("修复提示词生成失败: " + e.getMessage());
        }
    }
}
