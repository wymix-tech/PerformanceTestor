package com.perfserver.service;

import com.perfserver.entity.FixSuggestion;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.mapper.FixSuggestionMapper;
import com.perfserver.mapper.PerformanceIssueMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 修复回写（P2-2）：来源校验、方案解析、幂等、无场景快照时不发起重放。
 *
 * <p>幂等与"无快照不压测"是两条必须守住的分支：前者防重复点击白打一轮真实压力，
 * 后者防在无语义的情况下对着目标服务乱压。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class FixApplyServiceTest {

    @Mock
    private PerformanceIssueMapper issueMapper;

    @Mock
    private FixSuggestionMapper suggestionMapper;

    @Mock
    private IssueVerifyService issueVerifyService;

    private FixApplyService service;

    private PerformanceIssue issue;
    private FixSuggestion suggestion;

    @BeforeEach
    void setUp() {
        service = new FixApplyService(issueMapper, suggestionMapper, issueVerifyService);

        issue = new PerformanceIssue();
        issue.setId(11L);
        issue.setStatus("analyzed");
        issue.setStressTaskId(5L);

        suggestion = new FixSuggestion();
        suggestion.setId(21L);
        suggestion.setIssueId(11L);
        suggestion.setSelected(1);

        when(issueMapper.findById(11L)).thenReturn(issue);
        when(suggestionMapper.findById(21L)).thenReturn(suggestion);
        when(suggestionMapper.findByIssue(11L)).thenReturn(List.of(suggestion));
        lenient().when(suggestionMapper.updateApplied(anyLong(), any(), anyString(), anyString(), anyLong()))
                .thenReturn(1);
        lenient().when(issueVerifyService.verify(11L)).thenReturn(runningVerify());
    }

    private static IssueVerifyService.VerifyResult runningVerify() {
        IssueVerifyService.VerifyResult r = new IssueVerifyService.VerifyResult();
        r.issueId = 11L;
        r.running = true;
        r.replayTaskId = 77L;
        r.snapshotTaskId = 5L;
        r.message = "已发起重放压测";
        return r;
    }

    /** 正常回写：记录来源 + 触发重放验证，并落库 verifyTaskId */
    @Test
    void applyRecordsRefAndTriggersVerify() {
        FixApplyService.ApplyResult r = service.apply(11L, 21L, "pr", "https://git/pr/12");

        assertEquals(11L, r.issueId);
        assertEquals(21L, r.suggestionId);
        assertFalse(r.isIdempotent());
        assertEquals("pr", r.applyType);
        assertEquals("https://git/pr/12", r.applyRef);
        assertEquals(77L, r.verifyTaskId);
        assertNotNull(r.appliedAt);
        assertTrue(r.verify.running);
        verify(suggestionMapper).updateApplied(eq(21L), any(LocalDateTime.class),
                eq("https://git/pr/12"), eq("pr"), eq(77L));
    }

    /** 同来源重复提交：幂等返回，不重复触发重放压测 */
    @Test
    void repeatedSameRefIsIdempotent() {
        suggestion.setAppliedAt(LocalDateTime.now().minusMinutes(5));
        suggestion.setApplyType("pr");
        suggestion.setApplyRef("https://git/pr/12");
        suggestion.setVerifyTaskId(77L);
        when(issueVerifyService.verifyStatus(11L)).thenReturn(Map.of(
                "running", false, "passed", true, "message", "验证通过"));

        FixApplyService.ApplyResult r = service.apply(11L, 21L, "pr", "https://git/pr/12");

        assertTrue(r.isIdempotent());
        assertEquals(77L, r.verifyTaskId);
        verify(issueVerifyService, never()).verify(anyLong());
        verify(suggestionMapper, never()).updateApplied(anyLong(), any(), anyString(), anyString(), anyLong());
    }

    /** 换来源（又改了一版）：更新记录并重新触发验证 */
    @Test
    void changedRefRetriggersVerify() {
        suggestion.setAppliedAt(LocalDateTime.now().minusMinutes(5));
        suggestion.setApplyType("pr");
        suggestion.setApplyRef("https://git/pr/12");

        FixApplyService.ApplyResult r = service.apply(11L, 21L, "pr", "https://git/pr/13");

        assertFalse(r.isIdempotent());
        assertEquals("https://git/pr/13", r.applyRef);
        verify(issueVerifyService).verify(11L);
        verify(suggestionMapper).updateApplied(eq(21L), any(LocalDateTime.class),
                eq("https://git/pr/13"), eq("pr"), eq(77L));
    }

    /** 无压测场景快照：只记录回写，不发起重放压测（并说明原因） */
    @Test
    void noSnapshotSkipsVerify() {
        issue.setStressTaskId(0L);

        FixApplyService.ApplyResult r = service.apply(11L, 21L, "branch", "fix/n-plus-one");

        assertEquals(0L, r.verifyTaskId);
        assertTrue(r.message.contains("未关联压测场景快照"));
        verify(issueVerifyService, never()).verify(anyLong());
        verify(suggestionMapper).updateApplied(eq(21L), any(LocalDateTime.class),
                eq("fix/n-plus-one"), eq("branch"), eq(0L));
    }

    /** 验证触发异常：回写仍要落库（记录"改了什么"比"验证成功"更基础），错误如实上报 */
    @Test
    void verifyExceptionStillPersistsApply() {
        when(issueVerifyService.verify(11L)).thenThrow(new IllegalStateException("验证线程池已满"));

        FixApplyService.ApplyResult r = service.apply(11L, 21L, "branch", "fix/x");

        assertTrue(r.message.contains("触发异常"));
        assertEquals(0L, r.verifyTaskId);
        verify(suggestionMapper).updateApplied(anyLong(), any(), anyString(), anyString(), anyLong());
    }

    /** 未指定方案：取该问题已选定方案 */
    @Test
    void defaultSuggestionUsesSelected() {
        FixSuggestion first = new FixSuggestion();
        first.setId(20L);
        first.setIssueId(11L);
        first.setSelected(0);
        FixSuggestion selected = new FixSuggestion();
        selected.setId(22L);
        selected.setIssueId(11L);
        selected.setSelected(1);
        when(suggestionMapper.findByIssue(11L)).thenReturn(List.of(first, selected));

        FixApplyService.ApplyResult r = service.apply(11L, 0L, "patch", "补丁摘要");
        assertEquals(22L, r.suggestionId);
    }

    /** 未指定方案且无已选定：退首条 */
    @Test
    void defaultSuggestionFallsBackToFirst() {
        FixSuggestion first = new FixSuggestion();
        first.setId(20L);
        first.setIssueId(11L);
        first.setSelected(0);
        when(suggestionMapper.findByIssue(11L)).thenReturn(List.of(first));

        FixApplyService.ApplyResult r = service.apply(11L, 0L, "patch", "补丁摘要");
        assertEquals(20L, r.suggestionId);
    }

    /** 无任何方案：拒绝回写（没有地方记录来源） */
    @Test
    void noSuggestionRejected() {
        when(suggestionMapper.findByIssue(11L)).thenReturn(List.of());
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> service.apply(11L, 0L, "branch", "fix/x"));
        assertTrue(e.getMessage().contains("尚无修复方案"));
    }

    /** 非法来源类型：拒绝（不静默降级） */
    @Test
    void invalidApplyTypeRejected() {
        assertThrows(IllegalArgumentException.class, () -> service.apply(11L, 21L, "commit", "abc"));
        assertThrows(IllegalArgumentException.class, () -> service.apply(11L, 21L, "", "abc"));
        assertThrows(IllegalArgumentException.class, () -> service.apply(11L, 21L, null, "abc"));
        verify(suggestionMapper, never()).updateApplied(anyLong(), any(), anyString(), anyString(), anyLong());
    }

    /** 空来源标识：拒绝 */
    @Test
    void blankRefRejected() {
        assertThrows(IllegalArgumentException.class, () -> service.apply(11L, 21L, "branch", "   "));
        assertThrows(IllegalArgumentException.class, () -> service.apply(11L, 21L, "branch", null));
    }

    /** 超长来源（补丁全文）：截断摘要后落库，不能因超列宽报错 */
    @Test
    void longRefTruncated() {
        String huge = "patch".repeat(300);  // 1500 字符
        FixApplyService.ApplyResult r = service.apply(11L, 21L, "patch", huge);

        assertTrue(r.applyRef.length() <= 512, "来源标识必须裁剪到列宽以内");
        assertTrue(r.applyRef.endsWith("（已截断）"));
        verify(suggestionMapper).updateApplied(eq(21L), any(LocalDateTime.class),
                anyString(), eq("patch"), anyLong());
    }

    /** 来源类型大小写不敏感（PR / Pr 都归一化为 pr） */
    @Test
    void applyTypeNormalizedToLowercase() {
        FixApplyService.ApplyResult r = service.apply(11L, 21L, "PR", "https://git/pr/1");
        assertEquals("pr", r.applyType);
    }

    /** 方案不属于该问题：拒绝（防止跨问题串写） */
    @Test
    void suggestionFromOtherIssueRejected() {
        FixSuggestion other = new FixSuggestion();
        other.setId(99L);
        other.setIssueId(88L);
        when(suggestionMapper.findById(99L)).thenReturn(other);

        assertThrows(IllegalArgumentException.class, () -> service.apply(11L, 99L, "branch", "fix/x"));
    }

    /** 问题不存在：抛错 */
    @Test
    void missingIssueRejected() {
        when(issueMapper.findById(404L)).thenReturn(null);
        assertThrows(IllegalArgumentException.class, () -> service.apply(404L, 0L, "branch", "fix/x"));
    }

    /** 场景快照已失效：回写仍落库，verifyTaskId 为 0，消息说明未验证 */
    @Test
    void noSnapshotVerifyResultKeepsZeroTaskId() {
        IssueVerifyService.VerifyResult noSnap = new IssueVerifyService.VerifyResult();
        noSnap.issueId = 11L;
        noSnap.noSnapshot = true;
        noSnap.message = "场景快照压测任务 #5 已不存在";
        when(issueVerifyService.verify(11L)).thenReturn(noSnap);

        FixApplyService.ApplyResult r = service.apply(11L, 21L, "branch", "fix/x");

        assertEquals(0L, r.verifyTaskId);
        assertTrue(r.message.contains("场景快照已失效"));
        verify(suggestionMapper).updateApplied(eq(21L), any(LocalDateTime.class), eq("fix/x"), eq("branch"), eq(0L));
    }

    /** 幂等分支在无既有 verify_json 时不抛 NPE（verifyStatus 返回 null） */
    @Test
    void idempotentWithNoStoredVerifyJson() {
        suggestion.setAppliedAt(LocalDateTime.now());
        suggestion.setApplyType("branch");
        suggestion.setApplyRef("fix/x");
        when(issueVerifyService.verifyStatus(11L)).thenReturn(null);

        FixApplyService.ApplyResult r = service.apply(11L, 21L, "branch", "fix/x");
        assertTrue(r.isIdempotent());
        assertTrue(r.verify == null);
    }

    /** 幂等分支的既有结论被正确回放（passed/running 字段） */
    @Test
    void idempotentReplaysStoredVerify() {
        suggestion.setAppliedAt(LocalDateTime.now());
        suggestion.setApplyType("pr");
        suggestion.setApplyRef("https://git/pr/1");
        when(issueVerifyService.verifyStatus(11L)).thenReturn(Map.of(
                "running", false, "passed", true, "message", "验证通过，问题已关闭",
                "snapshotTaskId", 5, "replayTaskId", 88));

        FixApplyService.ApplyResult r = service.apply(11L, 21L, "pr", "https://git/pr/1");

        assertNotNull(r.verify);
        assertTrue(r.verify.passed);
        assertFalse(r.verify.running);
        assertEquals(88L, r.verify.replayTaskId);
    }

    /** suggestionId=0：走「已选定/首条」解析路径，不按 ID 精确查 */
    @Test
    void zeroSuggestionIdUsesResolvedSuggestion() {
        FixApplyService.ApplyResult r = service.apply(11L, 0L, "branch", "fix/y");
        assertEquals(21L, r.suggestionId);
        verify(suggestionMapper, never()).findById(anyLong());
    }
}
