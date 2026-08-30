package com.perfserver.ai;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.perfserver.ai.LlmClient.ChatResult;
import com.perfserver.ai.LlmClient.ToolCall;
import com.perfserver.service.MetricQueryService;
import com.perfserver.service.SourceIssueService;
import com.perfserver.service.StressTaskService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.mockito.ArgumentMatchers;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.atLeastOnce;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * ReAct 显式循环状态机：Thought → Action → Observation → Finish。
 *
 * <p>覆盖：工具轮次推进、观察值截断、轮次上限、未知工具降级、无 Key 规则降级、
 * 流式事件（含轮次心跳）与结论入库。
 */
class AiAgentReactLoopTest {

    private LlmClient llm;
    private AiKnowledgeService knowledge;
    private AiAgentService agent;
    private final Deque<ChatResult> scripted = new ArrayDeque<>();

    @BeforeEach
    @SuppressWarnings("unchecked")
    void setUp() {
        llm = mock(LlmClient.class);
        knowledge = mock(AiKnowledgeService.class);
        PromptBuilder promptBuilder = new PromptBuilder(List.of());
        AiTool echo = tool("echo", "{\"ok\":true}");
        agent = new AiAgentService(llm, promptBuilder, List.of(echo), knowledge,
                mock(MetricQueryService.class), mock(StressTaskService.class), mock(SourceIssueService.class));
        // 模拟流式：把 content 作为 token 推给 sink，同时返回完整结果
        // 注意：非流式调用第三个参数为 null，必须用 any() 而非 any(Class)（后者不匹配 null）
        when(llm.chatWithTools(anyList(), any(ArrayNode.class), ArgumentMatchers.<Consumer<String>>any()))
                .thenAnswer(inv -> {
                    ChatResult next = scripted.isEmpty() ? null : scripted.poll();
                    if (next == null) {
                        return null;
                    }
                    Consumer<String> sink = inv.getArgument(2);
                    if (sink != null && next.getContent() != null && !next.getContent().isBlank()) {
                        sink.accept(next.getContent());
                    }
                    return next;
                });
    }

    /** 工具轮次后给出结论：验证 FINISH 分支与工具留痕 */
    @Test
    void advancesThroughToolRoundThenFinishes() {
        scripted.add(result("先查慢 SQL 证据", call("echo", "{\"q\":\"慢SQL\"}")));
        scripted.add(result("根因：t_order 表缺少 (user_id,status) 复合索引，导致全表扫描，需改写为联表查询。"));

        var out = agent.chat("s1", "分析最近慢 SQL 的根因");

        assertEquals("根因：t_order 表缺少 (user_id,status) 复合索引，导致全表扫描，需改写为联表查询。", out.get("reply"));
        assertEquals("llm", out.get("mode"));
        assertEquals(AiRole.ROOT_CAUSE_ANALYST.name(), out.get("role"));
        assertEquals(1, out.get("rounds"));
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> toolCalls = (List<Map<String, Object>>) out.get("toolCalls");
        assertEquals(1, toolCalls.size());
        assertEquals("echo", toolCalls.get(0).get("name"));
        verify(llm, times(2)).chatWithTools(anyList(), any(ArrayNode.class), ArgumentMatchers.<Consumer<String>>any());
    }

    /** 观察值截断：工具返回超长时，回填上下文与前端预览都被限制在阈值内 */
    @Test
    void truncatesObservationBeforeFeedingBack() {
        AiAgentService longOutputAgent = new AiAgentService(llm, new PromptBuilder(List.of()),
                List.of(tool("big", "X".repeat(5000))), knowledge,
                mock(MetricQueryService.class), mock(StressTaskService.class), mock(SourceIssueService.class));
        scripted.add(result("", call("big", "{}")));
        scripted.add(result("已获取证据，慢 SQL 集中在订单列表查询，建议补充复合索引并改写联表查询。"));

        longOutputAgent.chat("s2", "分析最近慢 SQL 的根因");

        ArgumentCaptor<List<JsonNode>> captor = ArgumentCaptor.forClass(List.class);
        verify(llm, atLeastOnce()).chatWithTools(captor.capture(), any(ArrayNode.class), ArgumentMatchers.<Consumer<String>>any());
        JsonNode toolMsg = null;
        for (JsonNode node : captor.getAllValues().get(1)) {
            if ("tool".equals(node.path("role").asText())) {
                toolMsg = node;
            }
        }
        assertNotNull(toolMsg, "第二轮消息中应包含 tool 角色观察值");
        String observation = toolMsg.path("content").asText();
        assertTrue(observation.length() <= AiAgentService.MAX_OBSERVATION_CHARS + "...".length(),
                "观察值需截断到 " + AiAgentService.MAX_OBSERVATION_CHARS + " 字符，实际 " + observation.length());
        assertTrue(observation.endsWith("..."));
    }

    /** 轮次上限：模型持续请求工具时按 MAX_ROUNDS 收尾，不死循环 */
    @Test
    void stopsAtMaxRounds() {
        for (int i = 0; i < AiAgentService.MAX_ROUNDS + 5; i++) {
            scripted.add(result("", call("echo", "{}")));
        }

        var out = agent.chat("s3", "分析最近慢 SQL 的根因");

        verify(llm, times(AiAgentService.MAX_ROUNDS)).chatWithTools(anyList(), any(ArrayNode.class),
                ArgumentMatchers.<Consumer<String>>any());
        assertTrue(((String) out.get("reply")).contains("已达最大推理轮次"));
        assertEquals(AiAgentService.MAX_ROUNDS, out.get("rounds"));
    }

    /** 未知工具：转为 error 观察值继续推进，不让循环中断 */
    @Test
    void unknownToolBecomesErrorObservation() {
        scripted.add(result("", call("not_exist", "{}")));
        scripted.add(result("未获取到有效证据，慢 SQL 根因暂无法确认，请先确认应用已挂载探针上报数据。"));

        var out = agent.chat("s4", "分析最近慢 SQL 的根因");

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> toolCalls = (List<Map<String, Object>>) out.get("toolCalls");
        assertEquals(1, toolCalls.size());
        assertTrue(((String) toolCalls.get(0).get("result")).contains("未知工具: not_exist"));
        assertEquals("llm", out.get("mode"));
    }

    /** 无 API Key：降级规则模式，且不做结论入库（规则文本非模型结论） */
    @Test
    void fallsBackToRuleModeWhenLlmUnavailable() {
        // setUp 的 answer 在脚本为空时返回 null，等价于不可用/调用失败
        var out = agent.chat("s5", "你好");

        assertEquals("rule", out.get("mode"));
        assertFalse(((String) out.get("reply")).isBlank());
        verify(knowledge, never()).save(any(), anyString(), anyString(), anyString());
    }

    /** 结论入库：达标长度的模型结论在会话结束时写入知识库 */
    @Test
    void savesConclusionToKnowledgeBase() {
        scripted.add(result("", call("echo", "{}")));
        scripted.add(result("慢 SQL 根因为缺少复合索引，建议补充 (user_id,status) 索引并改写联表查询以降低耗时。"));

        agent.chat("s6", "分析最近慢 SQL 的根因", "perf-demo", null);

        verify(knowledge).save(eq("perf-demo"), anyString(), anyString(), anyString());
    }

    /** 短回复（寒暄/无信息量）不入库 */
    @Test
    void skipsKnowledgeSaveForShortReply() {
        scripted.add(result("好的"));

        agent.chat("s7", "你好", "perf-demo", null);

        verify(knowledge, never()).save(any(), anyString(), anyString(), anyString());
    }

    /** 流式事件顺序：轮次心跳 → 工具调用 → 观察值 → 结束 */
    @Test
    void emitsStreamEventsInReactOrder() {
        scripted.add(result("先查证据", call("echo", "{\"q\":1}")));
        scripted.add(result("慢 SQL 根因为缺少复合索引，建议补充索引并改写联表查询以降低查询耗时。"));
        List<AiStreamEvent> events = new ArrayList<>();

        agent.chat("s8", "分析最近慢 SQL 的根因", null, events::add);

        List<AiStreamEventType> types = events.stream().map(AiStreamEvent::type).toList();
        assertTrue(types.contains(AiStreamEventType.THOUGHT), "需推送轮次心跳防代理断连");
        assertTrue(types.contains(AiStreamEventType.TOOL));
        assertTrue(types.contains(AiStreamEventType.OBSERVATION));
        assertTrue(types.contains(AiStreamEventType.DONE));

        int firstThought = types.indexOf(AiStreamEventType.THOUGHT);
        int firstTool = types.indexOf(AiStreamEventType.TOOL);
        int firstObs = types.indexOf(AiStreamEventType.OBSERVATION);
        assertTrue(firstThought < firstTool, "心跳应在工具调用之前");
        assertTrue(firstTool < firstObs, "工具调用应在观察值之前");
        assertTrue(events.stream().anyMatch(e -> e.type() == AiStreamEventType.TOOL
                && e.payload().contains("echo")), "工具事件需携带工具名");
    }

    /** 角色切换：同一会话内 system prompt 原地替换，历史消息保留 */
    @Test
    void replacesSystemPromptWhenRoleChanges() {
        scripted.add(result("慢 SQL 根因为缺少复合索引，建议补充索引并改写联表查询以降低查询耗时。"));
        scripted.add(result("建议先补充复合索引，再改写联表查询，最后输出修复提示词供编码工具直接落地。"));

        agent.chat("s9", "分析最近慢 SQL 的根因");
        assertEquals(AiRole.ROOT_CAUSE_ANALYST, agent.roleOf("s9"));

        agent.chat("s9", "给出这个问题的修复提示词");
        assertEquals(AiRole.FIX_ADVISOR, agent.roleOf("s9"));

        ArgumentCaptor<List<JsonNode>> captor = ArgumentCaptor.forClass(List.class);
        verify(llm, atLeastOnce()).chatWithTools(captor.capture(), any(ArrayNode.class),
                ArgumentMatchers.<Consumer<String>>any());
        List<JsonNode> latest = captor.getAllValues().get(captor.getAllValues().size() - 1);
        assertEquals("system", latest.get(0).path("role").asText());
        assertTrue(latest.get(0).path("content").asText().contains("当前角色：修复建议"));
        // system 仍只有一条，且历史 user 消息未丢失
        assertEquals(1, latest.stream().filter(n -> "system".equals(n.path("role").asText())).count());
        assertEquals(2, latest.stream().filter(n -> "user".equals(n.path("role").asText())).count());
    }

    /** 清空会话：历史与角色状态一并移除 */
    @Test
    void clearResetsSessionAndRole() {
        scripted.add(result("慢 SQL 根因为缺少复合索引，建议补充索引并改写联表查询以降低查询耗时。"));
        agent.chat("s10", "分析最近慢 SQL 的根因");

        agent.clear("s10");

        assertEquals(AiRole.GENERAL, agent.roleOf("s10"));
    }

    // ---------------- helpers ----------------

    private static AiTool tool(String name, String output) {
        return new AiTool() {
            @Override
            public String name() {
                return name;
            }

            @Override
            public String description() {
                return "test tool " + name;
            }

            @Override
            public String parametersJson() {
                return "{\"type\":\"object\",\"properties\":{}}";
            }

            @Override
            public String execute(String jsonArgs) {
                return output;
            }
        };
    }

    private static ToolCall call(String name, String args) {
        ToolCall tc = new ToolCall();
        tc.setId("call-" + name);
        tc.setName(name);
        tc.setArguments(args);
        return tc;
    }

    private static ChatResult result(String content, ToolCall... calls) {
        ChatResult r = new ChatResult();
        r.setContent(content);
        for (ToolCall c : calls) {
            r.getToolCalls().add(c);
        }
        return r;
    }
}
