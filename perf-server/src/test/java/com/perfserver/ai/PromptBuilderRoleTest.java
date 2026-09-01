package com.perfserver.ai;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 三角色路由与 system prompt 组装。
 */
class PromptBuilderRoleTest {

    private final PromptBuilder builder = new PromptBuilder(List.of());

    @Test
    void routesClosedLoopInstructionToStressExecutor() {
        assertEquals(AiRole.STRESS_EXECUTOR,
                builder.detectRole("自动执行性能检测闭环：灌数→压测→观测→修复→报告"));
        assertEquals(AiRole.STRESS_EXECUTOR, builder.detectRole("一键检测这个应用"));
    }

    @Test
    void routesCauseQuestionToRootCauseAnalyst() {
        assertEquals(AiRole.ROOT_CAUSE_ANALYST, builder.detectRole("分析最近慢 SQL 的根因"));
        assertEquals(AiRole.ROOT_CAUSE_ANALYST, builder.detectRole("订单列表为什么这么慢"));
        assertEquals(AiRole.ROOT_CAUSE_ANALYST, builder.detectRole("当前系统的瓶颈在哪"));
    }

    @Test
    void routesFixRequestToFixAdvisor() {
        assertEquals(AiRole.FIX_ADVISOR, builder.detectRole("给出这个问题的修复提示词"));
        assertEquals(AiRole.FIX_ADVISOR, builder.detectRole("这段代码怎么改才能不慢"));
        assertEquals(AiRole.FIX_ADVISOR, builder.detectRole("帮我修复这个 N+1 问题"));
    }

    @Test
    void routesStressRequestToStressExecutor() {
        assertEquals(AiRole.STRESS_EXECUTOR, builder.detectRole("对 http://127.0.0.1:8081/api/list 压测，并发 20 持续 15 秒"));
        assertEquals(AiRole.STRESS_EXECUTOR, builder.detectRole("当前性能能承受多少并发、何时扩容"));
        assertEquals(AiRole.STRESS_EXECUTOR, builder.detectRole("向 demo 库灌入测试数据"));
    }

    @Test
    void fallsBackToGeneral() {
        assertEquals(AiRole.GENERAL, builder.detectRole(""));
        assertEquals(AiRole.GENERAL, builder.detectRole(null));
        assertEquals(AiRole.GENERAL, builder.detectRole("你好"));
    }

    @Test
    void rolePromptExtendsBaseWithRoleSection() {
        String base = builder.roleFor(AiRole.GENERAL);

        assertTrue(base.contains("search_knowledge"), "基座需声明知识库检索能力");
        assertTrue(base.contains("Thought → Action → Observation"), "基座需声明 ReAct 推理格式");

        String stress = builder.roleFor(AiRole.STRESS_EXECUTOR);
        String cause = builder.roleFor(AiRole.ROOT_CAUSE_ANALYST);
        String fix = builder.roleFor(AiRole.FIX_ADVISOR);

        assertTrue(stress.startsWith(base));
        assertTrue(cause.startsWith(base));
        assertTrue(fix.startsWith(base));

        assertTrue(cause.contains("【根因】"), "根因分析角色需给出固定结论结构");
        assertTrue(fix.contains("回滚方式"), "修复建议角色需给出风险与回滚");
        assertTrue(stress.contains("自动执行性能检测闭环"), "压测执行角色需保留闭环编排说明");

        assertNotEquals(stress, cause);
        assertNotEquals(cause, fix);
    }

    @Test
    void buildSystemPromptEqualsGeneralRole() {
        assertEquals(builder.roleFor(AiRole.GENERAL), builder.buildSystemPrompt());
    }
}
