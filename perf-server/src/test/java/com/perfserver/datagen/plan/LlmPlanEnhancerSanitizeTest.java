package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.ai.LlmClient;
import com.perftestor.datagen.model.EndpointTableFact;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

/**
 * LLM 增强脱敏守卫单测。
 *
 * <p>评审要求：LLM 只能收到结构元信息，禁止发送业务数据。本测试用<b>哨兵值</b>断言——
 * 在事实中放入可识别的字面量，再验证提示词中绝不出现它们。任何把原始值透传出去的改动都会在此失败。</p>
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class LlmPlanEnhancerSanitizeTest {

    /** 哨兵：业务数据绝不能外泄 */
    private static final String SECRET_ORDER_NO = "ORD_20260902_99887766";
    private static final String SECRET_PHONE = "13800001111";
    private static final String SECRET_SQL = "SELECT * FROM perf_order WHERE order_no = 'ORD_20260902_99887766'";
    private static final String SECRET_CONN = "jdbc:mysql://10.0.0.7:3306/prod?password=Abc123456";

    @Mock
    private LlmClient llmClient;

    private LlmPlanEnhancer enhancer() {
        return new LlmPlanEnhancer(llmClient, new ObjectMapper());
    }

    private static EndpointTableFact factWithSecrets() {
        EndpointTableFact f = new EndpointTableFact("perf_order", EndpointTableFact.SqlAccess.READ);
        EndpointTableFact.Predicate p = new EndpointTableFact.Predicate();
        p.setColumn("order_no");
        p.setOp(EndpointTableFact.Op.EQ);
        p.getValues().add(SECRET_ORDER_NO);
        f.getPredicates().add(p);

        EndpointTableFact.Predicate phone = new EndpointTableFact.Predicate();
        phone.setColumn("phone");
        phone.setOp(EndpointTableFact.Op.IN);
        phone.getValues().add(SECRET_PHONE);
        f.getPredicates().add(phone);
        f.setUnresolvedReason(SECRET_SQL);
        return f;
    }

    @Test
    void promptNeverContainsBusinessValues() {
        String prompt = enhancer().sanitizedPrompt(List.of(factWithSecrets()));
        assertFalse(LlmPlanEnhancer.containsLiteral(prompt, SECRET_ORDER_NO),
                "业务单号绝不能出现在 LLM 提示词中");
        assertFalse(LlmPlanEnhancer.containsLiteral(prompt, SECRET_PHONE),
                "手机号绝不能出现在 LLM 提示词中");
    }

    @Test
    void promptNeverContainsSqlOrConnectionString() {
        String prompt = enhancer().sanitizedPrompt(List.of(factWithSecrets()));
        assertFalse(LlmPlanEnhancer.containsLiteral(prompt, SECRET_SQL), "SQL 全文禁止外发");
        assertFalse(LlmPlanEnhancer.containsLiteral(prompt, SECRET_CONN), "连接串禁止外发");
        assertFalse(prompt.contains("10.0.0.7"), "数据库主机禁止外发");
    }

    @Test
    void valuesAreReplacedByTypePlaceholders() {
        String prompt = enhancer().sanitizedPrompt(List.of(factWithSecrets()));
        assertTrue(prompt.contains("<str:len="), "字符串值应替换为长度占位，实际: " + prompt);
    }

    @Test
    void structuralMetadataIsKept() {
        String prompt = enhancer().sanitizedPrompt(List.of(factWithSecrets()));
        assertTrue(prompt.contains("perf_order"), "表名属于结构元信息，应保留");
        assertTrue(prompt.contains("order_no"), "列名属于结构元信息，应保留");
        assertTrue(prompt.contains("READ"), "读写类型属于结构元信息，应保留");
    }

    @Test
    void valueShapeCoversNumericAndDate() {
        assertEquals("<int>", LlmPlanEnhancer.shapeOf(42L));
        assertEquals("<decimal>", LlmPlanEnhancer.shapeOf(1.5d));
        assertEquals("<date>", LlmPlanEnhancer.shapeOf("2026-09-02"));
        assertTrue(LlmPlanEnhancer.shapeOf("abc").startsWith("<str:len="));
        assertEquals("<null>", LlmPlanEnhancer.shapeOf(null));
    }

    @Test
    void disabledEnhancementIsNotApplied() {
        var out = enhancer().enhance(List.of(factWithSecrets()), false);
        assertFalse(out.isApplied());
        assertTrue(out.getReason().contains("未开启"));
    }

    @Test
    void missingApiKeyDegradesSilentlyWithReason() {
        when(llmClient.available()).thenReturn(false);
        var out = enhancer().enhance(List.of(factWithSecrets()), true);
        assertFalse(out.isApplied(), "无 API Key 必须降级");
        assertTrue(out.getReason().contains("降级"), "降级原因应可展示给用户: " + out.getReason());
    }

    @Test
    void nullLlmResponseDegradesWithReason() {
        when(llmClient.available()).thenReturn(true);
        when(llmClient.chatJson(anyString(), anyString(), any())).thenReturn(null);
        var out = enhancer().enhance(List.of(factWithSecrets()), true);
        assertFalse(out.isApplied());
        assertTrue(out.getReason().contains("降级"));
    }

    @Test
    void llmExceptionDegradesNotThrow() {
        when(llmClient.available()).thenReturn(true);
        when(llmClient.chatJson(anyString(), anyString(), any()))
                .thenThrow(new RuntimeException("timeout"));
        var out = enhancer().enhance(List.of(factWithSecrets()), true);
        assertFalse(out.isApplied());
        assertTrue(out.getReason().contains("降级"), "异常必须降级，不得中断分析: " + out.getReason());
    }

    @Test
    void appliedEnhancementWarnsUserToReview() {
        when(llmClient.available()).thenReturn(true);
        when(llmClient.chatJson(anyString(), anyString(), any())).thenReturn("{\"tables\":[]}");
        var out = enhancer().enhance(List.of(factWithSecrets()), true);
        assertTrue(out.isApplied());
        assertTrue(out.getWarnings().stream().anyMatch(w -> w.contains("复核")),
                "LLM 增强应用后必须提示复核: " + out.getWarnings());
    }

    @Test
    void emptyFactsSkipEnhancement() {
        var out = enhancer().enhance(List.of(), true);
        assertFalse(out.isApplied());
        assertTrue(out.getReason().contains("跳过"));
    }

    @Test
    void tableCountIsCapped() {
        // 构造超过上限的事实，验证提示词长度受控
        java.util.List<EndpointTableFact> many = new java.util.ArrayList<>();
        for (int i = 0; i < LlmPlanEnhancer.MAX_TABLES + 30; i++) {
            many.add(new EndpointTableFact("table_" + i, EndpointTableFact.SqlAccess.READ));
        }
        String prompt = enhancer().sanitizedPrompt(many);
        assertFalse(prompt.contains("table_" + (LlmPlanEnhancer.MAX_TABLES + 5)),
                "超过上限的表不应进入提示词（控制成本与长度）");
    }
}
