package com.perfserver.source;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.ai.LlmClient;
import com.perfserver.entity.DbConn;
import com.perfserver.entity.FixSuggestion;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.entity.SlowSql;
import com.perfserver.entity.SourceFile;
import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.DbConnMapper;
import com.perfserver.mapper.FixSuggestionMapper;
import com.perfserver.mapper.PerformanceIssueMapper;
import com.perfserver.mapper.SlowSqlMapper;
import com.perfserver.mapper.SourceFileMapper;
import com.perfserver.security.CryptoService;
import com.perfserver.service.DbExplainService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/**
 * 性能问题分析器：组装上下文（检测证据 + 源码片段 + 表结构 + 链路占比）调用大模型，
 * JSON Schema 约束输出 2-3 个修复方案并落库。
 * 无 API Key 时降级规则模板模式，保证可演示。
 */
@Service
public class PerformanceAnalyzer {

    private static final Logger log = LoggerFactory.getLogger(PerformanceAnalyzer.class);

    private final LlmClient llmClient;
    private final PerformanceIssueMapper issueMapper;
    private final FixSuggestionMapper suggestionMapper;
    private final SourceFileMapper sourceFileMapper;
    private final SourceFileService sourceFileService;
    private final SourceManager sourceManager;
    private final SlowSqlMapper slowSqlMapper;
    private final DbConnMapper dbConnMapper;
    private final DbExplainService dbExplainService;
    private final CryptoService cryptoService;
    private final ObjectMapper objectMapper = new ObjectMapper();

    public PerformanceAnalyzer(LlmClient llmClient, PerformanceIssueMapper issueMapper,
                               FixSuggestionMapper suggestionMapper, SourceFileMapper sourceFileMapper,
                               SourceFileService sourceFileService, SourceManager sourceManager,
                               SlowSqlMapper slowSqlMapper, DbConnMapper dbConnMapper,
                               DbExplainService dbExplainService, CryptoService cryptoService) {
        this.llmClient = llmClient;
        this.issueMapper = issueMapper;
        this.suggestionMapper = suggestionMapper;
        this.sourceFileMapper = sourceFileMapper;
        this.sourceFileService = sourceFileService;
        this.sourceManager = sourceManager;
        this.slowSqlMapper = slowSqlMapper;
        this.dbConnMapper = dbConnMapper;
        this.dbExplainService = dbExplainService;
        this.cryptoService = cryptoService;
    }

    /**
     * 对问题执行根因分析并生成 2-3 个修复方案，落库 performance_issue 与 fix_suggestion。
     * slow_sql 问题会先尝试按已保存的数据库连接执行 EXPLAIN/SHOW INDEX 等只读检测，
     * 将真实执行计划并入分析上下文与检测证据。
     *
     * @return 生成的方案数（可能为 0）
     */
    public int analyze(long issueId) {
        PerformanceIssue issue = issueMapper.findById(issueId);
        if (issue == null) {
            throw new IllegalArgumentException("问题不存在: " + issueId);
        }
        // 慢 SQL：尝试数据库只读检测（EXPLAIN 等），结果并入证据与上下文
        String dbExplainText = collectDbExplain(issue);
        String context = buildContext(issue, dbExplainText);
        String system = """
                你是一个资深 Java 性能优化专家。根据检测证据、数据库执行计划与源码上下文，分析性能问题根因，
                并给出 2-3 个可落地的修复方案。方案必须具体到文件与改动点。
                若提供了 EXPLAIN 执行计划，请基于 type/key/rows/Extra 等字段给出确诊结论（如全表扫描、未命中索引、filesort）。
                只输出 JSON，不要输出任何其他文字。
                """;
        String user = "请分析以下性能问题并生成修复方案：\n\n" + context;

        JsonNode schema = buildSchema();
        String json = llmClient.chatJson(system, user, schema);
        if (json == null) {
            // 降级规则模板
            return fallbackAnalyze(issue);
        }
        return parseAndSave(issue, json);
    }

    /**
     * 慢 SQL 数据库只读检测：按证据中的 slowSqlId 找到 SQL，
     * 按 dbHost 匹配已保存连接（无匹配取默认），执行 EXPLAIN 等并更新证据。返回可读摘要。
     */
    private String collectDbExplain(PerformanceIssue issue) {
        if (!"slow_sql".equals(issue.getIssueType()) || issue.getEvidenceJson() == null) {
            return "";
        }
        try {
            JsonNode evidence = objectMapper.readTree(issue.getEvidenceJson());
            long slowSqlId = evidence.path("slowSqlId").asLong(0);
            if (slowSqlId <= 0) {
                return "";
            }
            var slowSql = slowSqlMapper.findById(slowSqlId);
            if (slowSql == null) {
                return "";
            }
            DbConn conn = matchConn(evidence.path("dbHost").asText(""), slowSql.getAppName());
            if (conn == null) {
                return "";
            }
            // 取用点解密：库内凭据为密文，明文不入 AI 上下文与日志
            Map<String, Object> explain = dbExplainService.analyzeSql(
                    conn.getDbUrl(), conn.getUsername(),
                    cryptoService.decrypt(conn.getPassword()), slowSql.getSqlText());
            String summary = dbExplainService.formatSummary(explain);
            if (summary.isBlank()) {
                return "";
            }
            String actualDb = evidence.path("dbHost").asText("");
            String explainTarget = dbExplainService.urlHost(conn.getDbUrl());
            // 一致性标注：EXPLAIN 库与慢 SQL 实际执行库不一致时前端警告（结果可能失真）
            boolean consistent = actualDb.isBlank()
                    || explainTarget.isBlank()
                    || actualDb.contains(explainTarget) || explainTarget.contains(actualDb);
            ObjectNode dbExplainNode = ((ObjectNode) evidence).putObject("dbExplain");
            dbExplainNode.set("summary", objectMapper.valueToTree(summary));
            dbExplainNode.put("target", explainTarget);
            dbExplainNode.put("actualDb", actualDb);
            dbExplainNode.put("consistent", consistent);
            issueMapper.updateEvidenceJson(issue.getId(), objectMapper.writeValueAsString(evidence));
            issue.setEvidenceJson(objectMapper.writeValueAsString(evidence));
            return summary;
        } catch (Exception e) {
            log.warn("collect db explain failed for issue {}: {}", issue.getId(), e.getMessage());
            return "";
        }
    }

    /**
     * 连接匹配三级链（EXPLAIN 必须落在慢 SQL 实际执行的库上才有意义，事实优先）：
     * <ol>
     *   <li>host 匹配（事实优先）：慢 SQL dbHost 是 agent 从真实连接 URL 提取的，
     *       与配置 host 双向包含命中即为实际执行库</li>
     *   <li>应用绑定（声明式路由）：dbHost 匹配不上时（应用迁移了库 / slowlog 来源无 host），
     *       按"该应用现在用的连接"路由——绑定连接的 host 应与实际库保持一致</li>
     *   <li>默认兜底：is_default=1 优先，否则第一条（此时 EXPLAIN 库可能与实际库不一致，
     *       证据中会标注 consistent=false 供前端警告）</li>
     * </ol>
     */
    private DbConn matchConn(String dbHost, String appName) {
        List<DbConn> conns = dbConnMapper.matchOrder();
        if (conns.isEmpty()) {
            return null;
        }
        if (dbHost != null && !dbHost.isBlank()) {
            for (DbConn c : conns) {
                if (c.getHost() != null && !c.getHost().isBlank()
                        && (dbHost.contains(c.getHost()) || c.getHost().contains(dbHost))) {
                    return c;
                }
            }
        }
        if (appName != null && !appName.isBlank()) {
            for (DbConn c : conns) {
                if (c.getAppName() != null && !c.getAppName().isBlank()
                        && appName.equals(c.getAppName())) {
                    return c;
                }
            }
        }
        return conns.get(0);
    }

    /** 组装上下文：检测证据 + 数据库执行计划 + 源码片段 */
    private String buildContext(PerformanceIssue issue, String dbExplainText) {
        StringBuilder sb = new StringBuilder();
        sb.append("【问题类型】").append(issue.getIssueType()).append("\n");
        sb.append("【标题】").append(issue.getTitle()).append("\n");
        sb.append("【检测证据】").append(issue.getEvidenceJson() == null ? "无" : issue.getEvidenceJson()).append("\n");
        if (!dbExplainText.isBlank()) {
            sb.append("\n【数据库执行计划（真实 EXPLAIN）】\n").append(dbExplainText).append("\n");
        }

        if (issue.getSourceRepoId() != null && issue.getSourceFileId() != null) {
            try {
                SourceRepo repo = sourceManager.getRepo(issue.getSourceRepoId());
                SourceFile sf = sourceFileMapper.findById(issue.getSourceFileId());
                if (sf != null) {
                    Path root = sourceManager.getRepoRoot(repo);
                    int lineStart = issue.getLineStart() == null ? 1 : issue.getLineStart();
                    int lineEnd = issue.getLineEnd() == null ? lineStart + 20 : issue.getLineEnd();
                    String snippet = sourceFileService.readLines(root, sf, lineStart, lineEnd);
                    sb.append("\n【关联源码】").append(sf.getRelativePath())
                            .append(" (第 ").append(lineStart).append("-").append(lineEnd).append(" 行)\n")
                            .append(snippet);
                }
            } catch (IOException e) {
                log.warn("read source snippet failed for issue {}: {}", issue.getId(), e.getMessage());
            }
        }
        return sb.toString();
    }

    /** JSON Schema：约束输出 2-3 个方案 */
    private JsonNode buildSchema() {
        ObjectNode schema = objectMapper.createObjectNode();
        schema.put("name", "analysis_result");
        schema.put("strict", true);
        ObjectNode root = schema.putObject("schema");
        root.put("type", "object");
        ObjectNode props = root.putObject("properties");
        props.put("root_cause", objectMapper.createObjectNode().put("type", "string").put("description", "问题根因分析"));
        ArrayNode solutions = props.putArray("solutions");
        ObjectNode solutionItem = solutions.addObject();
        solutionItem.put("type", "object");
        ObjectNode sp = solutionItem.putObject("properties");
        sp.put("title", objectMapper.createObjectNode().put("type", "string").put("description", "方案名称"));
        sp.put("files", objectMapper.createObjectNode().put("type", "array").put("description", "涉及文件清单"));
        sp.put("changes", objectMapper.createObjectNode().put("type", "array").put("description", "具体改动点"));
        sp.put("code_changes", objectMapper.createObjectNode().put("type", "array").put("description", "关键代码改动要点"));
        sp.put("benefit", objectMapper.createObjectNode().put("type", "string").put("description", "收益"));
        sp.put("risk", objectMapper.createObjectNode().put("type", "string").put("description", "风险"));
        solutionItem.putArray("required").add("title").add("files").add("changes").add("benefit").add("risk");
        root.putArray("required").add("root_cause").add("solutions");
        return schema;
    }

    /** 解析大模型输出并落库 */
    private int parseAndSave(PerformanceIssue issue, String json) {
        try {
            // 剥离可能的 markdown 代码围栏
            String cleaned = json.trim();
            if (cleaned.startsWith("```")) {
                cleaned = cleaned.replaceFirst("^```[a-zA-Z]*\\n?", "").replaceFirst("\\n?```$", "");
            }
            JsonNode root = objectMapper.readTree(cleaned);
            String rootCause = root.path("root_cause").asText("");
            issueMapper.updateAnalyzed(issue.getId(), rootCause);

            JsonNode solutions = root.path("solutions");
            List<FixSuggestion> list = new java.util.ArrayList<>();
            for (JsonNode s : solutions) {
                FixSuggestion fs = new FixSuggestion();
                fs.setIssueId(issue.getId());
                fs.setTitle(s.path("title").asText("修复方案"));
                fs.setFilesJson(nodeToJson(s.path("files")));
                fs.setChangesJson(nodeToJson(s.path("changes")));
                fs.setCodeChangesJson(nodeToJson(s.path("code_changes")));
                fs.setBenefit(s.path("benefit").asText(""));
                fs.setRisk(s.path("risk").asText(""));
                fs.setSelected(0);
                list.add(fs);
                if (list.size() >= 3) {
                    break;
                }
            }
            if (!list.isEmpty()) {
                suggestionMapper.batchInsert(list);
            }
            return list.size();
        } catch (Exception e) {
            log.warn("parse llm analysis failed: {}", e.getMessage());
            return 0;
        }
    }

    private String nodeToJson(JsonNode node) {
        return node == null || node.isMissingNode() ? "[]" : node.toString();
    }

    /** 规则降级：无 API Key 时按问题类型生成模板方案 */
    private int fallbackAnalyze(PerformanceIssue issue) {
        String rootCause = "（规则模式）" + templateRootCause(issue.getIssueType());
        issueMapper.updateAnalyzed(issue.getId(), rootCause);

        FixSuggestion fs = new FixSuggestion();
        fs.setIssueId(issue.getId());
        fs.setTitle(templateTitle(issue.getIssueType()));
        fs.setFilesJson("[{\"file\":\"待确认\",\"action\":\"按检测证据定位\"}]");
        fs.setChangesJson("[{\"point\":\"优化热点执行路径，减少重复计算与无效调用\"},{\"point\":\"增加缓存或索引，降低耗时\"}]");
        fs.setCodeChangesJson("[]");
        fs.setBenefit("显著降低该路径耗时，提升整体吞吐");
        fs.setRisk("需回归验证，防止引入数据一致性问题");
        fs.setSelected(0);
        suggestionMapper.batchInsert(List.of(fs));

        FixSuggestion fs2 = new FixSuggestion();
        fs2.setIssueId(issue.getId());
        fs2.setTitle("异步化与削峰（规则模式）");
        fs2.setFilesJson("[{\"file\":\"待确认\",\"action\":\"定位热点方法\"}]");
        fs2.setChangesJson("[{\"point\":\"将非关键路径改为异步执行，削峰填谷\"},{\"point\":\"必要时引入消息队列解耦\"}]");
        fs2.setCodeChangesJson("[]");
        fs2.setBenefit("提升系统弹性与稳定性");
        fs2.setRisk("异步化增加复杂度，需关注失败补偿");
        fs2.setSelected(0);
        suggestionMapper.batchInsert(List.of(fs2));
        return 2;
    }

    private String templateRootCause(String type) {
        return switch (type == null ? "" : type) {
            case "slow_sql" -> "SQL 查询未命中索引或扫描行数过大，导致单次查询耗时偏高";
            case "hot_method" -> "热点方法存在重复计算/循环内调用，执行路径耗时占比过高";
            case "stress_bottleneck" -> "压测结果显示吞吐达到上限，存在串行瓶颈或资源争用";
            case "jvm_alert" -> "堆内存或 GC 频率异常，可能存在内存泄漏或配置不当";
            default -> "结合检测证据初步判断为执行路径耗时过高";
        };
    }

    private String templateTitle(String type) {
        return switch (type == null ? "" : type) {
            case "slow_sql" -> "SQL 优化：索引与查询改写";
            case "hot_method" -> "热点方法优化：消除重复计算";
            case "stress_bottleneck" -> "瓶颈消除：并发与资源调优";
            case "jvm_alert" -> "JVM 参数与内存优化";
            default -> "执行路径优化";
        };
    }
}
