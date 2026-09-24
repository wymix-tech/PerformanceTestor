package com.perfserver.service;

import com.perfserver.entity.FixSuggestion;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.mapper.FixSuggestionMapper;
import com.perfserver.mapper.PerformanceIssueMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Set;

/**
 * 修复回写（P2-2）：把"方案已落到代码"这件事从人工复制升级为平台记录，并自动串起闭环验证。
 *
 * <p>原闭环终点是「生成修复提示词」，之后全靠人工复制到 IDE、手工再点一次验证，
 * 中间"改了什么、改在哪、验证的是哪一版"没有任何记录。本服务补上这一段：
 * <ol>
 *   <li>记录修复来源（分支名 / PR 链接 / 补丁摘要，三选一）；</li>
 *   <li>把对应 {@code fix_suggestion} 标记为已回写（{@code applied_at} / {@code apply_ref}）；</li>
 *   <li>复用 {@link IssueVerifyService} 自动触发一次重放验证（问题需已绑定压测场景快照）。</li>
 * </ol>
 *
 * <p><b>幂等</b>：同一问题重复 apply 时，若来源与类型与既有记录一致，直接返回既有结论，
 * 不重复触发重放压测（一次重放就是对目标服务打一轮真实压力，不能因重复点击白打）。
 * 来源不同则视为"又改了一版"，更新记录并重新触发验证。
 */
@Service
public class FixApplyService {

    private static final Logger log = LoggerFactory.getLogger(FixApplyService.class);

    /** 支持的修复来源类型 */
    private static final Set<String> APPLY_TYPES = Set.of("branch", "pr", "patch");
    /** 来源标识最大长度（与 fix_suggestion.apply_ref 列宽一致） */
    private static final int MAX_REF_LEN = 512;
    /** PR 链接/补丁摘要存入前截断用的摘要长度 */
    private static final int REF_SUMMARY_LEN = 500;

    private final PerformanceIssueMapper issueMapper;
    private final FixSuggestionMapper suggestionMapper;
    private final IssueVerifyService issueVerifyService;

    public FixApplyService(PerformanceIssueMapper issueMapper, FixSuggestionMapper suggestionMapper,
                           IssueVerifyService issueVerifyService) {
        this.issueMapper = issueMapper;
        this.suggestionMapper = suggestionMapper;
        this.issueVerifyService = issueVerifyService;
    }

    /** 回写结果：已落库的修复来源 + 自动验证结论（无场景快照时 verify=null） */
    public static class ApplyResult {
        public long issueId;
        public long suggestionId;
        /** 本次是否为重复提交（来源一致，未重复触发验证） */
        public boolean idempotent;
        public String applyType;
        public String applyRef;
        public LocalDateTime appliedAt;
        /** 回写后触发的重放验证任务 ID（0 = 未触发） */
        public Long verifyTaskId;
        /** 自动验证结论（进行中/失败/达标）；问题未绑定场景快照时为 null */
        public IssueVerifyService.VerifyResult verify;
        /** 提示信息（是否已触发验证、为何未触发） */
        public String message;

        public long getIssueId() {
            return issueId;
        }

        public long getSuggestionId() {
            return suggestionId;
        }

        public boolean isIdempotent() {
            return idempotent;
        }

        public String getApplyType() {
            return applyType;
        }

        public String getApplyRef() {
            return applyRef;
        }

        public LocalDateTime getAppliedAt() {
            return appliedAt;
        }

        public Long getVerifyTaskId() {
            return verifyTaskId;
        }

        public IssueVerifyService.VerifyResult getVerify() {
            return verify;
        }

        public String getMessage() {
            return message;
        }
    }

    /**
     * 回写修复并触发验证。
     *
     * @param issueId      问题 ID
     * @param suggestionId 方案 ID（0 = 不指定，取该问题已选定方案；仍无则取首条）
     * @param applyType    来源类型：branch / pr / patch
     * @param applyRef     来源标识（分支名 / PR 链接 / 补丁摘要），必填
     */
    public ApplyResult apply(long issueId, long suggestionId, String applyType, String applyRef) {
        PerformanceIssue issue = issueMapper.findById(issueId);
        if (issue == null) {
            throw new IllegalArgumentException("问题不存在: " + issueId);
        }
        String type = normalizeType(applyType);
        String ref = normalizeRef(applyRef);

        FixSuggestion suggestion = resolveSuggestion(issueId, suggestionId);

        // 幂等：同问题 + 同来源 + 同类型 → 直接返回既有记录，不重复发起重放压测
        String existingType = suggestion.getApplyType() == null ? "" : suggestion.getApplyType();
        String existingRef = suggestion.getApplyRef() == null ? "" : suggestion.getApplyRef();
        if (suggestion.getAppliedAt() != null && existingType.equals(type) && existingRef.equals(ref)) {
            ApplyResult r = new ApplyResult();
            r.issueId = issueId;
            r.suggestionId = suggestion.getId();
            r.idempotent = true;
            r.applyType = existingType;
            r.applyRef = existingRef;
            r.appliedAt = suggestion.getAppliedAt();
            r.verifyTaskId = suggestion.getVerifyTaskId() == null ? 0L : suggestion.getVerifyTaskId();
            r.verify = issueVerifyService.verifyStatus(issueId) == null ? null
                    : toVerifyResult(issueVerifyService.verifyStatus(issueId));
            r.message = "该修复来源此前已回写，未重复触发验证（可在「验证结论」查看最近一次结果）";
            return r;
        }

        // 触发验证：问题有压测场景快照才有意义；无快照时只记录回写，如实说明未验证的原因
        IssueVerifyService.VerifyResult verify = null;
        Long verifyTaskId = 0L;
        String message = "修复已回写，自动验证已发起（重放完成后自动更新结论）";
        if (issue.getStressTaskId() == null || issue.getStressTaskId() <= 0) {
            message = "修复已回写；该问题未关联压测场景快照，无法自动重放验证。"
                    + "可先「补绑压测场景快照」后再执行验证";
        } else {
            try {
                verify = issueVerifyService.verify(issueId);
                verifyTaskId = verify != null && verify.replayTaskId != null ? verify.replayTaskId : 0L;
                if (verify != null && verify.noSnapshot) {
                    message = "修复已回写；场景快照已失效（" + verify.message + "），未触发验证";
                } else if (verify != null && verify.replayFailed) {
                    message = "修复已回写；自动验证触发失败：" + verify.message;
                }
            } catch (Exception e) {
                log.warn("apply-fix verify trigger failed, issue={}: {}", issueId, e.getMessage());
                message = "修复已回写；自动验证触发异常：" + e.getMessage();
            }
        }

        LocalDateTime appliedAt = LocalDateTime.now();
        suggestionMapper.updateApplied(suggestion.getId(), appliedAt, ref, type, verifyTaskId);

        ApplyResult r = new ApplyResult();
        r.issueId = issueId;
        r.suggestionId = suggestion.getId();
        r.applyType = type;
        r.applyRef = ref;
        r.appliedAt = appliedAt;
        r.verifyTaskId = verifyTaskId;
        r.verify = verify;
        r.message = message;
        log.info("修复回写: issue={}, suggestion={}, type={}, ref={}, verifyTask={}",
                issueId, suggestion.getId(), type, ref, verifyTaskId);
        return r;
    }

    /** 方案解析：显式指定 → 该问题已选定方案 → 首条方案；三者皆无则报错（无处记录回写） */
    private FixSuggestion resolveSuggestion(long issueId, long suggestionId) {
        if (suggestionId > 0) {
            FixSuggestion s = suggestionMapper.findById(suggestionId);
            if (s == null || s.getIssueId() == null || s.getIssueId() != issueId) {
                throw new IllegalArgumentException("方案不存在或不属于该问题: " + suggestionId);
            }
            return s;
        }
        List<FixSuggestion> list = suggestionMapper.findByIssue(issueId);
        if (list == null || list.isEmpty()) {
            throw new IllegalArgumentException(
                    "该问题尚无修复方案，无法回写：请先执行分析生成方案，或在请求体指定 suggestionId");
        }
        return list.stream()
                .filter(s -> s.getSelected() != null && s.getSelected() == 1)
                .findFirst()
                .orElse(list.get(0));
    }

    /** 类型归一化：空 / 非法值一律拒绝（静默降级会让回写记录失去区分度） */
    private static String normalizeType(String applyType) {
        String t = applyType == null ? "" : applyType.trim().toLowerCase(java.util.Locale.ROOT);
        if (t.isEmpty()) {
            throw new IllegalArgumentException("修复来源类型不能为空，可选：branch / pr / patch");
        }
        if (!APPLY_TYPES.contains(t)) {
            throw new IllegalArgumentException("不支持的修复来源类型：" + applyType + "，可选：branch / pr / patch");
        }
        return t;
    }

    /** 来源标识归一化：必填；补丁类超长内容只留摘要（列宽 512，不截断会直接报错） */
    private static String normalizeRef(String applyRef) {
        String ref = applyRef == null ? "" : applyRef.trim();
        if (ref.isEmpty()) {
            throw new IllegalArgumentException("修复来源标识不能为空（分支名 / PR 链接 / 补丁摘要）");
        }
        if (ref.length() > MAX_REF_LEN) {
            ref = ref.substring(0, REF_SUMMARY_LEN) + "…（已截断）";
        }
        return ref;
    }

    /** 既有 verify_json 结论 → VerifyResult（幂等返回时复用展示） */
    private static IssueVerifyService.VerifyResult toVerifyResult(java.util.Map<String, Object> parsed) {
        IssueVerifyService.VerifyResult r = new IssueVerifyService.VerifyResult();
        r.running = Boolean.TRUE.equals(parsed.get("running"));
        r.passed = Boolean.TRUE.equals(parsed.get("passed"));
        r.replayFailed = Boolean.TRUE.equals(parsed.get("replayFailed"));
        Object msg = parsed.get("message");
        r.message = msg == null ? "" : String.valueOf(msg);
        r.snapshotTaskId = asLong(parsed.get("snapshotTaskId"));
        r.replayTaskId = asLong(parsed.get("replayTaskId"));
        return r;
    }

    private static Long asLong(Object v) {
        if (v instanceof Number n) {
            return n.longValue();
        }
        try {
            return Long.parseLong(String.valueOf(v));
        } catch (Exception e) {
            return null;
        }
    }
}
