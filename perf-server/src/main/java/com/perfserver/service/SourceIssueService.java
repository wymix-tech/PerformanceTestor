package com.perfserver.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.entity.FixSuggestion;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.entity.SlowSql;
import com.perfserver.entity.SourceRepo;
import com.perfserver.entity.StressTask;
import com.perfserver.mapper.FixSuggestionMapper;
import com.perfserver.mapper.PerformanceIssueMapper;
import com.perfserver.mapper.SlowSqlMapper;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.source.FixPromptGenerator;
import com.perfserver.source.IssueAssociator;
import com.perfserver.source.PerformanceAnalyzer;
import com.perfserver.source.SourceManager;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 性能问题编排服务：从检测数据（慢 SQL / 热点方法 / 压测任务）创建问题，
 * 关联源码位置，驱动大模型分析与修复提示词生成。
 */
@Service
public class SourceIssueService {

    private static final Logger log = LoggerFactory.getLogger(SourceIssueService.class);

    private final SlowSqlMapper slowSqlMapper;
    private final StressTaskMapper stressTaskMapper;
    private final PerformanceIssueMapper issueMapper;
    private final FixSuggestionMapper suggestionMapper;
    private final SourceManager sourceManager;
    private final IssueAssociator associator;
    private final PerformanceAnalyzer analyzer;
    private final FixPromptGenerator promptGenerator;
    private final ObjectMapper objectMapper = new ObjectMapper();

    public SourceIssueService(SlowSqlMapper slowSqlMapper, StressTaskMapper stressTaskMapper,
                              PerformanceIssueMapper issueMapper, FixSuggestionMapper suggestionMapper,
                              SourceManager sourceManager, IssueAssociator associator,
                              PerformanceAnalyzer analyzer, FixPromptGenerator promptGenerator) {
        this.slowSqlMapper = slowSqlMapper;
        this.stressTaskMapper = stressTaskMapper;
        this.issueMapper = issueMapper;
        this.suggestionMapper = suggestionMapper;
        this.sourceManager = sourceManager;
        this.associator = associator;
        this.analyzer = analyzer;
        this.promptGenerator = promptGenerator;
    }

    /** 从慢 SQL 创建问题并关联源码 */
    public PerformanceIssue createFromSlowSql(long slowSqlId, long repoId) {
        SlowSql sql = findSlowSql(slowSqlId);
        SourceRepo repo = sourceManager.getRepo(repoId);
        IssueAssociator.IssueLocation loc = associator.associateSlowSql(repo, sql.getSqlText());

        PerformanceIssue issue = new PerformanceIssue();
        issue.setIssueType("slow_sql");
        issue.setAppName(sql.getAppName());
        issue.setTitle("慢 SQL: " + truncate(sql.getSqlText(), 60));
        issue.setEvidenceJson(buildEvidence(sql));
        issue.setStatus("pending");
        if (loc != null) {
            issue.setSourceRepoId(loc.repoId);
            issue.setSourceFileId(loc.fileId);
            issue.setLineStart(loc.lineStart);
            issue.setLineEnd(loc.lineEnd);
        } else {
            issue.setSourceRepoId(repoId);
        }
        issueMapper.insert(issue);
        return issue;
    }

    /** 从热点方法创建问题（人工/AI 指定类名方法名） */
    public PerformanceIssue createFromHotMethod(String appName, String className, String methodName, long repoId) {
        SourceRepo repo = sourceManager.getRepo(repoId);
        IssueAssociator.IssueLocation loc = associator.associateHotMethod(repo, className, methodName);

        PerformanceIssue issue = new PerformanceIssue();
        issue.setIssueType("hot_method");
        issue.setAppName(appName == null ? "unknown" : appName);
        issue.setTitle("热点方法耗时高: " + className + "#" + methodName);
        ObjectNode evidence = objectMapper.createObjectNode();
        evidence.put("className", className);
        evidence.put("methodName", methodName);
        issue.setEvidenceJson(evidence.toString());
        issue.setStatus("pending");
        if (loc != null) {
            issue.setSourceRepoId(loc.repoId);
            issue.setSourceFileId(loc.fileId);
            issue.setLineStart(loc.lineStart);
            issue.setLineEnd(loc.lineEnd);
        } else {
            issue.setSourceRepoId(repoId);
        }
        issueMapper.insert(issue);
        return issue;
    }

    /** 从压测任务创建问题（URL 关联 Controller） */
    public PerformanceIssue createFromStress(long stressTaskId, long repoId) {
        StressTask task = stressTaskMapper.findById(stressTaskId);
        if (task == null) {
            throw new IllegalArgumentException("压测任务不存在: " + stressTaskId);
        }
        SourceRepo repo = sourceManager.getRepo(repoId);
        IssueAssociator.IssueLocation loc = associator.associateUrl(repo, task.getTargetUrl());

        PerformanceIssue issue = new PerformanceIssue();
        issue.setIssueType("stress_bottleneck");
        issue.setAppName("stress");
        issue.setTitle("压测瓶颈: " + task.getTargetUrl() + " (并发 " + task.getConcurrency() + ")");
        ObjectNode evidence = objectMapper.createObjectNode();
        evidence.put("taskId", task.getId());
        evidence.put("targetUrl", task.getTargetUrl());
        evidence.put("concurrency", task.getConcurrency());
        evidence.put("resultJson", task.getResultJson() == null ? "{}" : task.getResultJson());
        issue.setEvidenceJson(evidence.toString());
        issue.setStatus("pending");
        if (loc != null) {
            issue.setSourceRepoId(loc.repoId);
            issue.setSourceFileId(loc.fileId);
            issue.setLineStart(loc.lineStart);
            issue.setLineEnd(loc.lineEnd);
        } else {
            issue.setSourceRepoId(repoId);
        }
        issueMapper.insert(issue);
        return issue;
    }

    /** 查询问题列表 */
    public List<PerformanceIssue> listIssues(String issueType, String status, int limit) {
        return issueMapper.query(issueType, status, Math.max(1, Math.min(limit, 200)));
    }

    /** 问题详情（含方案列表） */
    public IssueDetail getIssueDetail(long issueId) {
        PerformanceIssue issue = issueMapper.findById(issueId);
        if (issue == null) {
            throw new IllegalArgumentException("问题不存在: " + issueId);
        }
        IssueDetail detail = new IssueDetail();
        detail.issue = issue;
        detail.suggestions = suggestionMapper.findByIssue(issueId);
        return detail;
    }

    /** 执行大模型分析（生成根因与多方案） */
    public int analyze(long issueId) {
        return analyzer.analyze(issueId);
    }

    /** 选定方案并生成修复提示词 */
    public String selectSuggestion(long issueId, long suggestionId) {
        return promptGenerator.generate(issueId, suggestionId);
    }

    /** 查询最近慢 SQL */
    public List<SlowSql> recentSlowSqls(String app, int limit) {
        return slowSqlMapper.query(app, LocalDateTime.now().minusDays(7), LocalDateTime.now(), null,
                Math.max(1, Math.min(limit, 50)));
    }

    private SlowSql findSlowSql(long id) {
        List<SlowSql> list = slowSqlMapper.query(null, LocalDateTime.now().minusDays(7),
                LocalDateTime.now(), null, 1000);
        return list.stream().filter(s -> s.getId() != null && s.getId() == id).findFirst()
                .orElseThrow(() -> new IllegalArgumentException("慢 SQL 不存在: " + id));
    }

    private String buildEvidence(SlowSql sql) {
        ObjectNode node = objectMapper.createObjectNode();
        node.put("slowSqlId", sql.getId());
        node.put("appName", sql.getAppName());
        node.put("sqlText", sql.getSqlText());
        node.put("costMs", sql.getCostMs());
        node.put("source", sql.getSource());
        node.put("dbHost", sql.getDbHost());
        return node.toString();
    }

    private String truncate(String s, int max) {
        if (s == null) {
            return "";
        }
        return s.length() <= max ? s : s.substring(0, max) + "...";
    }

    /** 问题详情视图对象 */
    public static class IssueDetail {
        public PerformanceIssue issue;
        public List<FixSuggestion> suggestions;

        public PerformanceIssue getIssue() {
            return issue;
        }

        public List<FixSuggestion> getSuggestions() {
            return suggestions;
        }
    }
}
