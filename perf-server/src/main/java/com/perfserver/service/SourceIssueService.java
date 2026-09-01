package com.perfserver.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.common.Texts;
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

    /** 从慢 SQL 创建问题并关联源码（优先用触发方法链精确定位，退回 SQL 文本模糊匹配） */
    public PerformanceIssue createFromSlowSql(long slowSqlId, long repoId) {
        SlowSql sql = findSlowSql(slowSqlId);
        SourceRepo repo = sourceManager.getRepo(repoId);
        // ORM 无关多级定位：方法链（首选）→ MyBatis XML → JPA/Hibernate 实体
        IssueAssociator.IssueLocation loc =
                associator.associateSlowSqlSmart(repo, sql.getSqlText(), sql.getMethodChain());

        PerformanceIssue issue = new PerformanceIssue();
        issue.setIssueType("slow_sql");
        issue.setAppName(sql.getAppName());
        issue.setTitle("慢 SQL: " + Texts.truncate(sql.getSqlText(), 60));
        issue.setEvidenceJson(buildEvidence(sql));
        issue.setStatus("pending");
        return insertIssue(issue, loc, repoId);
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
        return insertIssue(issue, loc, repoId);
    }

    /**
     * 从压测任务创建问题（URL 关联 Controller）。
     * 应用继承链：调用方显式指定 → 压测任务自带 appName（压测时已按项目绑定记录）→ 回退 stress。
     */
    public PerformanceIssue createFromStress(long stressTaskId, long repoId, String appName) {
        StressTask task = stressTaskMapper.findById(stressTaskId);
        if (task == null) {
            throw new IllegalArgumentException("压测任务不存在: " + stressTaskId);
        }
        SourceRepo repo = sourceManager.getRepo(repoId);
        IssueAssociator.IssueLocation loc = associator.associateUrl(repo, task.getTargetUrl());

        String resolvedApp = appName != null && !appName.isBlank() ? appName
                : task.getAppName() != null && !task.getAppName().isBlank() ? task.getAppName() : "stress";

        PerformanceIssue issue = new PerformanceIssue();
        issue.setIssueType("stress_bottleneck");
        issue.setAppName(resolvedApp);
        issue.setTitle("压测瓶颈: " + task.getTargetUrl() + " (并发 " + task.getConcurrency() + ")");
        // 场景快照：记录问题发现时的压测任务，作为修复后一键重放验证的基线
        issue.setStressTaskId(stressTaskId);
        ObjectNode evidence = objectMapper.createObjectNode();
        evidence.put("taskId", task.getId());
        evidence.put("targetUrl", task.getTargetUrl());
        evidence.put("concurrency", task.getConcurrency());
        evidence.put("resultJson", task.getResultJson() == null ? "{}" : task.getResultJson());
        issue.setEvidenceJson(evidence.toString());
        issue.setStatus("pending");
        return insertIssue(issue, loc, repoId);
    }

    /** 统一收尾：命中定位则写入源码位置，否则仅绑仓库；随后落库返回 */
    private PerformanceIssue insertIssue(PerformanceIssue issue, IssueAssociator.IssueLocation loc, long repoId) {
        // 未关联压测场景时显式落 0（列 NOT NULL，避免 null 入参）
        if (issue.getStressTaskId() == null) {
            issue.setStressTaskId(0L);
        }
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

    /** 查询问题列表（可按应用/类型/状态过滤） */
    public List<PerformanceIssue> listIssues(String appName, String issueType, String status, int limit) {
        return issueMapper.query(appName, issueType, status, Math.max(1, Math.min(limit, 200)));
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

    /**
     * 执行大模型分析（生成根因与多方案）。
     * 压测瓶颈问题必须已关联源码仓库（分析需定位 Controller 源码），否则拒绝。
     */
    public int analyze(long issueId) {
        PerformanceIssue issue = issueMapper.findById(issueId);
        if (issue == null) {
            throw new IllegalArgumentException("问题不存在: " + issueId);
        }
        if ("stress_bottleneck".equals(issue.getIssueType()) && issue.getSourceRepoId() == null) {
            throw new IllegalArgumentException(
                    "压测问题必须先关联源码仓库才能分析：请先完成「关联源码仓库」再执行分析");
        }
        return analyzer.analyze(issueId);
    }

    /**
     * 为已存在问题补绑源码仓库并自动定位（压测问题分析的前置操作）。
     * 按问题类型选择定位策略：压测 → URL 关联；慢 SQL → 方法链/XML/实体；热点方法 → 类名/方法名。
     *
     * @return 更新后的问题详情（含定位结果）
     */
    public IssueDetail bindRepo(long issueId, long repoId) {
        PerformanceIssue issue = issueMapper.findById(issueId);
        if (issue == null) {
            throw new IllegalArgumentException("问题不存在: " + issueId);
        }
        SourceRepo repo = sourceManager.getRepo(repoId);
        String type = issue.getIssueType();
        String url = null;
        String sqlText = null;
        String chain = null;
        String className = null;
        String methodName = null;
        try {
            JsonNode ev = objectMapper.readTree(issue.getEvidenceJson() == null ? "{}" : issue.getEvidenceJson());
            url = ev.path("targetUrl").asText(null);
            sqlText = ev.path("sqlText").asText(null);
            chain = ev.path("methodChain").isMissingNode() ? null : ev.path("methodChain").toString();
            className = ev.path("className").asText(null);
            methodName = ev.path("methodName").asText(null);
        } catch (Exception ignored) {
            // 证据缺失时仅落仓库级关联
        }
        IssueAssociator.IssueLocation loc = null;
        if ("stress_bottleneck".equals(type) && url != null) {
            loc = associator.associateUrl(repo, url);
        } else if ("slow_sql".equals(type)) {
            loc = associator.associateSlowSqlSmart(repo, sqlText, chain);
        } else if ("hot_method".equals(type) && className != null) {
            loc = associator.associateHotMethod(repo, className, methodName);
        }
        issueMapper.updateLocation(issueId, repoId,
                loc == null ? null : loc.fileId,
                loc == null ? null : loc.lineStart,
                loc == null ? null : loc.lineEnd);
        return getIssueDetail(issueId);
    }

    /**
     * 为已存在问题补绑压测场景快照（一键验证的前置条件）。
     * 用于慢 SQL / 热点方法等非压测来源的问题：选定一个同场景压测任务作为验证基线。
     */
    public PerformanceIssue bindStressTask(long issueId, long stressTaskId) {
        PerformanceIssue issue = issueMapper.findById(issueId);
        if (issue == null) {
            throw new IllegalArgumentException("问题不存在: " + issueId);
        }
        if (stressTaskId <= 0) {
            throw new IllegalArgumentException("压测任务 ID 非法: " + stressTaskId);
        }
        if (stressTaskMapper.findById(stressTaskId) == null) {
            throw new IllegalArgumentException("压测任务不存在: " + stressTaskId);
        }
        issueMapper.updateStressTask(issueId, stressTaskId);
        issue.setStressTaskId(stressTaskId);
        return issue;
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
        SlowSql sql = slowSqlMapper.findById(id);
        if (sql == null) {
            throw new IllegalArgumentException("慢 SQL 不存在: " + id);
        }
        return sql;
    }

    private String buildEvidence(SlowSql sql) {
        ObjectNode node = objectMapper.createObjectNode();
        node.put("slowSqlId", sql.getId());
        node.put("appName", sql.getAppName());
        node.put("sqlText", sql.getSqlText());
        node.put("costMs", sql.getCostMs());
        node.put("source", sql.getSource());
        node.put("dbHost", sql.getDbHost());
        // 触发方法链：让根因分析与提示词都知道"这条 SQL 由哪个业务链路触发"
        if (sql.getMethodChain() != null && !sql.getMethodChain().isBlank()) {
            try {
                node.set("methodChain", objectMapper.readTree(sql.getMethodChain()));
            } catch (Exception ignored) {
                node.put("methodChain", sql.getMethodChain());
            }
        }
        return node.toString();
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
