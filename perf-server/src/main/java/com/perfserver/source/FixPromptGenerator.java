package com.perfserver.source;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.FixSuggestion;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.entity.SourceFile;
import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.FixSuggestionMapper;
import com.perfserver.mapper.PerformanceIssueMapper;
import com.perfserver.mapper.SourceFileMapper;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.nio.file.Path;

/**
 * 修复提示词生成器：将选定方案组装为结构化 Markdown，
 * 供 AI 辅助编码工具直接使用（复制/下载）。
 */
@Service
public class FixPromptGenerator {

    private final PerformanceIssueMapper issueMapper;
    private final FixSuggestionMapper suggestionMapper;
    private final SourceFileMapper sourceFileMapper;
    private final SourceFileService sourceFileService;
    private final SourceManager sourceManager;
    private final ObjectMapper objectMapper = new ObjectMapper();

    public FixPromptGenerator(PerformanceIssueMapper issueMapper, FixSuggestionMapper suggestionMapper,
                              SourceFileMapper sourceFileMapper, SourceFileService sourceFileService,
                              SourceManager sourceManager) {
        this.issueMapper = issueMapper;
        this.suggestionMapper = suggestionMapper;
        this.sourceFileMapper = sourceFileMapper;
        this.sourceFileService = sourceFileService;
        this.sourceManager = sourceManager;
    }

    /**
     * 生成修复提示词 Markdown 并落库（fix_suggestion.prompt_md）。
     *
     * @param issueId      问题 id
     * @param suggestionId 选定方案 id
     * @return Markdown 文本
     */
    public String generate(long issueId, long suggestionId) {
        PerformanceIssue issue = issueMapper.findById(issueId);
        if (issue == null) {
            throw new IllegalArgumentException("问题不存在: " + issueId);
        }
        FixSuggestion suggestion = suggestionMapper.findById(suggestionId);
        if (suggestion == null || suggestion.getIssueId() != issueId) {
            throw new IllegalArgumentException("方案不存在或不属于该问题: " + suggestionId);
        }
        String md = buildMarkdown(issue, suggestion);
        suggestionMapper.updateSelected(suggestionId, 1);
        suggestionMapper.updatePromptMd(suggestionId, md);
        return md;
    }

    private String buildMarkdown(PerformanceIssue issue, FixSuggestion suggestion) {
        StringBuilder sb = new StringBuilder();
        sb.append("# 性能问题修复任务\n\n");
        sb.append("## 1. 任务目标\n\n");
        sb.append("修复性能问题：**").append(issue.getTitle()).append("**（类型：")
                .append(issue.getIssueType()).append("）\n\n");
        sb.append("## 2. 检测证据\n\n");
        sb.append(issue.getEvidenceJson() == null ? "无\n\n" : "```json\n" + issue.getEvidenceJson() + "\n```\n\n");
        sb.append("## 3. 根因分析\n\n");
        sb.append(issue.getRootCause() == null ? "（规则模式）待进一步确认\n\n" : issue.getRootCause() + "\n\n");

        // 源码引用
        if (issue.getSourceRepoId() != null && issue.getSourceFileId() != null) {
            try {
                SourceRepo repo = sourceManager.getRepo(issue.getSourceRepoId());
                SourceFile sf = sourceFileMapper.findById(issue.getSourceFileId());
                if (sf != null) {
                    Path root = sourceManager.getRepoRoot(repo);
                    int lineStart = issue.getLineStart() == null ? 1 : issue.getLineStart();
                    int lineEnd = issue.getLineEnd() == null ? lineStart + 20 : issue.getLineEnd();
                    String snippet = sourceFileService.readLines(root, sf, lineStart, lineEnd);
                    sb.append("## 4. 源码引用\n\n");
                    sb.append("文件：`").append(sf.getRelativePath()).append("` 第 ").append(lineStart)
                            .append("-").append(lineEnd).append(" 行\n\n```java\n")
                            .append(snippet).append("```\n\n");
                }
            } catch (IOException e) {
                sb.append("（源码片段读取失败）\n\n");
            }
        }

        sb.append("## 5. 建议修改\n\n");
        sb.append("目标文件：\n\n");
        sb.append("- ").append(formatJson(suggestion.getFilesJson())).append("\n\n");
        sb.append("修改点：\n\n");
        sb.append("- ").append(formatJson(suggestion.getChangesJson())).append("\n\n");
        if (suggestion.getCodeChangesJson() != null && !suggestion.getCodeChangesJson().isBlank()
                && !"[]".equals(suggestion.getCodeChangesJson())) {
            sb.append("关键代码改动：\n\n```json\n")
                    .append(suggestion.getCodeChangesJson()).append("\n```\n\n");
        }

        sb.append("## 6. 预期收益\n\n");
        sb.append(suggestion.getBenefit()).append("\n\n");
        sb.append("## 7. 风险与注意事项\n\n");
        sb.append(suggestion.getRisk()).append("\n\n");
        sb.append("## 8. 验证方式\n\n");
        sb.append("- 重新压测/复跑慢查询，确认耗时下降\n");
        sb.append("- 回归业务功能，确认无数据一致性问题\n");
        sb.append("- 观察 JVM 指标与错误率，确认无新异常\n\n");
        sb.append("> 请按上述方案修改代码，保持现有代码风格，改动最小化，并补充必要注释。");
        return sb.toString();
    }

    private String formatJson(String json) {
        if (json == null || json.isBlank()) {
            return "待确认";
        }
        try {
            JsonNode node = objectMapper.readTree(json);
            if (node.isArray()) {
                StringBuilder sb = new StringBuilder();
                for (JsonNode item : node) {
                    String file = item.path("file").asText();
                    String action = item.path("action").asText();
                    sb.append("`").append(file).append("`（").append(action).append("）；");
                }
                if (sb.length() > 0) {
                    sb.setLength(sb.length() - 1);
                }
                return sb.toString();
            }
            return json;
        } catch (Exception e) {
            return json;
        }
    }
}
