package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.service.SourceIssueService;
import org.springframework.stereotype.Component;

import java.util.Map;

/**
 * 代码问题分析工具：从检测证据（慢 SQL/热点方法/压测 URL）创建性能问题并调用大模型生成多方案。
 */
@Component
public class AnalyzeCodeIssuesTool extends AbstractAiTool {

    private final SourceIssueService sourceIssueService;

    public AnalyzeCodeIssuesTool(SourceIssueService sourceIssueService) {
        this.sourceIssueService = sourceIssueService;
    }

    @Override
    public String name() {
        return "analyze_code_issues";
    }

    @Override
    public String description() {
        return "分析性能问题根因并生成 2-3 个修复方案。参数：source 必填（slow_sql 慢 SQL 定位 / hot_method 热点方法定位 / "
                + "stress 压测 URL 定位）；repoId 源码仓库 ID（可选，默认 1）；慢 SQL 定位需 slowSqlId；热点方法定位需 className 与 "
                + "methodName（及可选 appName）；压测定位需 stressTaskId。返回问题 ID、根因与生成的方案列表（含涉及文件/修改点/收益/风险）。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"source", "repoId", "slowSqlId", "className", "methodName", "appName", "stressTaskId"},
                new String[]{"source"}).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        String source = str(args, "source", "");
        long repoId = lng(args, "repoId", 1L);
        try {
            PerformanceIssue issue;
            switch (source) {
                case "slow_sql" -> issue = sourceIssueService.createFromSlowSql(
                        lng(args, "slowSqlId", 0L), repoId);
                case "hot_method" -> issue = sourceIssueService.createFromHotMethod(
                        str(args, "appName", null), str(args, "className", ""), str(args, "methodName", ""), repoId);
                case "stress" -> issue = sourceIssueService.createFromStress(
                        lng(args, "stressTaskId", 0L), repoId);
                default -> {
                    return error("source 仅支持 slow_sql / hot_method / stress");
                }
            }
            int suggestionCount = sourceIssueService.analyze(issue.getId());
            var detail = sourceIssueService.getIssueDetail(issue.getId());
            return ok(Map.of(
                    "issueId", issue.getId(),
                    "title", issue.getTitle(),
                    "rootCause", detail.getIssue().getRootCause(),
                    "suggestionCount", suggestionCount,
                    "suggestions", detail.getSuggestions()));
        } catch (Exception e) {
            return error("问题分析失败: " + e.getMessage());
        }
    }
}
