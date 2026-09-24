package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.SourceRepoMapper;
import com.perfserver.service.SourceIssueService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * analyze_code_issues 的 repoId 防呆：缺失/非法一律不落库，回可用仓库清单。
 *
 * <p>过去默认 {@code repoId=1L}，用户一句"分析这个慢 SQL"就可能把问题记录
 * 建到无关仓库上。这些用例锁死"不确定就不写库"这条底线。
 */
class AnalyzeCodeIssuesToolTest {

    private final ObjectMapper json = new ObjectMapper();

    private SourceIssueService issueService;
    private SourceRepoMapper repoMapper;
    private AnalyzeCodeIssuesTool tool;

    @BeforeEach
    void setUp() {
        issueService = mock(SourceIssueService.class);
        repoMapper = mock(SourceRepoMapper.class);
        tool = new AnalyzeCodeIssuesTool(issueService, repoMapper);
    }

    private static SourceRepo repo(long id, String name) {
        SourceRepo r = new SourceRepo();
        r.setId(id);
        r.setRepoName(name);
        r.setAppName(name);
        return r;
    }

    /** IssueDetail 是只读视图对象（字段 public、无 setter），测试直接填字段 */
    private static SourceIssueService.IssueDetail detail(PerformanceIssue issue) {
        SourceIssueService.IssueDetail detail = new SourceIssueService.IssueDetail();
        detail.issue = issue;
        detail.suggestions = List.of();
        return detail;
    }

    private JsonNode parse(String result) {
        try {
            return json.readTree(result);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    // ==================== repoId 缺失 / 非法 ====================

    /** 缺 repoId：不调用任何创建逻辑，返回可用仓库清单 */
    @Test
    void missingRepoIdReturnsRepoOptionsWithoutCreating() throws Exception {
        when(repoMapper.query(50)).thenReturn(List.of(repo(1L, "perf-demo"), repo(2L, "perf-server")));

        JsonNode result = parse(tool.execute("{\"source\":\"slow_sql\",\"slowSqlId\":7}"));

        assertTrue(result.has("error"), "缺 repoId 应报错让模型向用户确认");
        assertFalse(result.has("data"), "不得返回分析结果，说明确实没落库");
        String message = result.get("error").asText();
        assertTrue(message.contains("repoId"), "错误信息应点明 repoId 必填，实际 " + message);
        assertTrue(message.contains("perf-demo"), "应带可用仓库清单供追问，实际 " + message);
        verify(issueService, never()).createFromSlowSql(anyLong(), anyLong());
        verify(issueService, never()).analyze(anyLong());
    }

    /** repoId=0 / 负数同样视为缺失 */
    @Test
    void zeroOrNegativeRepoIdRejected() {
        when(repoMapper.query(50)).thenReturn(List.of());

        assertTrue(parse(tool.execute("{\"source\":\"slow_sql\",\"repoId\":0}")).has("error"));
        assertTrue(parse(tool.execute("{\"source\":\"slow_sql\",\"repoId\":-5}")).has("error"));
        verify(issueService, never()).createFromSlowSql(anyLong(), anyLong());
    }

    /** repoId 不存在：不允许建到未确认的仓库上 */
    @Test
    void unknownRepoIdRejected() {
        when(repoMapper.findById(999L)).thenReturn(null);
        when(repoMapper.query(50)).thenReturn(List.of(repo(1L, "perf-demo")));

        JsonNode result = parse(tool.execute("{\"source\":\"slow_sql\",\"repoId\":999,\"slowSqlId\":1}"));

        assertTrue(result.has("error"));
        assertTrue(result.get("error").asText().contains("perf-demo"));
        verify(issueService, never()).createFromSlowSql(anyLong(), anyLong());
    }

    /** 仓库回查异常按"不可用"处理：宁可不建，也不建到不确定的仓库 */
    @Test
    void repoLookupFailureRejected() {
        when(repoMapper.findById(1L)).thenThrow(new RuntimeException("db down"));
        when(repoMapper.query(50)).thenReturn(List.of());

        assertTrue(parse(tool.execute("{\"source\":\"slow_sql\",\"repoId\":1}")).has("error"));
        verify(issueService, never()).createFromSlowSql(anyLong(), anyLong());
    }

    /** 清单查询也异常：仍返回 error（清单为空数组），不能冒异常 */
    @Test
    void repoOptionsFailureStillReturnsError() {
        when(repoMapper.query(50)).thenThrow(new RuntimeException("db down"));

        JsonNode result = parse(tool.execute("{\"source\":\"slow_sql\"}"));

        assertTrue(result.has("error"));
    }

    // ==================== source 校验 ====================

    @Test
    void unsupportedSourceRejected() {
        JsonNode result = parse(tool.execute("{\"source\":\"magic\",\"repoId\":1}"));
        assertTrue(result.get("error").asText().contains("slow_sql"));
        verify(repoMapper, never()).findById(anyLong());
    }

    @Test
    void blankSourceRejected() {
        assertTrue(parse(tool.execute("{\"repoId\":1}")).has("error"));
    }

    // ==================== 正常路径 ====================

    @Test
    void validRepoIdCreatesIssueForSlowSql() throws Exception {
        when(repoMapper.findById(3L)).thenReturn(repo(3L, "perf-demo"));
        PerformanceIssue issue = new PerformanceIssue();
        issue.setId(42L);
        issue.setTitle("慢 SQL: select * from t_order");
        when(issueService.createFromSlowSql(7L, 3L)).thenReturn(issue);
        when(issueService.analyze(42L)).thenReturn(2);
        when(issueService.getIssueDetail(42L)).thenReturn(detail(issue));

        JsonNode result = parse(tool.execute("{\"source\":\"slow_sql\",\"slowSqlId\":7,\"repoId\":3}"));

        assertEquals(42L, result.get("data").get("issueId").asLong());
        assertEquals(2, result.get("data").get("suggestionCount").asInt());
        verify(issueService).createFromSlowSql(7L, 3L);
    }

    @Test
    void validRepoIdCreatesHotMethodIssue() {
        when(repoMapper.findById(3L)).thenReturn(repo(3L, "perf-demo"));
        PerformanceIssue issue = new PerformanceIssue();
        issue.setId(9L);
        // appName 缺省为 null：不能用 anyString()（它不匹配 null），否则桩不生效
        when(issueService.createFromHotMethod(any(), anyString(), anyString(), anyLong())).thenReturn(issue);
        when(issueService.analyze(9L)).thenReturn(1);
        when(issueService.getIssueDetail(9L)).thenReturn(detail(issue));

        JsonNode result = parse(tool.execute(
                "{\"source\":\"hot_method\",\"repoId\":3,\"className\":\"A\",\"methodName\":\"b\"}"));

        assertEquals(9L, result.get("data").get("issueId").asLong());
    }

    @Test
    void validRepoIdCreatesStressIssue() {
        when(repoMapper.findById(2L)).thenReturn(repo(2L, "perf-demo"));
        PerformanceIssue issue = new PerformanceIssue();
        issue.setId(5L);
        when(issueService.createFromStress(anyLong(), anyLong(), any())).thenReturn(issue);
        when(issueService.analyze(5L)).thenReturn(3);
        when(issueService.getIssueDetail(5L)).thenReturn(detail(issue));

        JsonNode result = parse(tool.execute("{\"source\":\"stress\",\"repoId\":2,\"stressTaskId\":11}"));

        assertEquals(5L, result.get("data").get("issueId").asLong());
    }

    /** 分析过程抛异常：返回 error 而不是把异常抛给编排层（ReAct 循环要能继续推进） */
    @Test
    void analyzeFailureReturnsError() {
        when(repoMapper.findById(3L)).thenReturn(repo(3L, "perf-demo"));
        when(issueService.createFromSlowSql(anyLong(), anyLong())).thenThrow(new RuntimeException("llm timeout"));

        JsonNode result = parse(tool.execute("{\"source\":\"slow_sql\",\"repoId\":3,\"slowSqlId\":1}"));

        assertTrue(result.get("error").asText().contains("llm timeout"));
    }

    /** 参数 JSON 本身非法：按空参数处理，走 repoId 缺失分支而不是崩掉 */
    @Test
    void malformedJsonHandledAsMissingRepoId() {
        when(repoMapper.query(50)).thenReturn(List.of());
        assertTrue(parse(tool.execute("not-json")).has("error"));
    }

    // ==================== Schema ====================

    /** repoId 必须在 required 里，否则模型会认为可选并省略它 */
    @Test
    void repoIdDeclaredRequiredInSchema() throws Exception {
        JsonNode schema = json.readTree(tool.parametersJson());
        JsonNode required = schema.get("required");
        assertTrue(required.toString().contains("repoId"), "repoId 必须声明为必填，实际 " + required);
        assertTrue(required.toString().contains("source"));
    }
}
