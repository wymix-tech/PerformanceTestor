package com.perfserver.ai;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.ai.LlmClient.ChatResult;
import com.perfserver.ai.LlmClient.ToolCall;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 流式响应累加：正文增量拼接与 tool_calls 分片合并（OpenAI 兼容 stream=true）。
 */
class StreamAccumulatorTest {

    private final ObjectMapper mapper = new ObjectMapper();
    private StreamAccumulator acc;

    @BeforeEach
    void setUp() {
        acc = new StreamAccumulator();
    }

    /** 正文 delta：逐块返回 token，并拼接为完整内容 */
    @Test
    void concatenatesContentDeltas() {
        List<String> t1 = acc.consume("{\"choices\":[{\"delta\":{\"content\":\"根因是\"}}]}", mapper);
        List<String> t2 = acc.consume("{\"choices\":[{\"delta\":{\"content\":\"缺少索引\"}}]}", mapper);

        assertEquals(List.of("根因是"), t1);
        assertEquals(List.of("缺少索引"), t2);
        assertEquals("根因是缺少索引", acc.toResult().getContent());
        assertTrue(acc.toResult().getToolCalls().isEmpty());
    }

    /** 工具调用分片：id/name 只在首块，arguments 逐块拼接 */
    @Test
    void mergesIncrementalToolCallArguments() {
        acc.consume("{\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\","
                + "\"function\":{\"name\":\"slow_sql\",\"arguments\":\"\"}}]}}]}", mapper);
        acc.consume("{\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,"
                + "\"function\":{\"arguments\":\"{\\\"min\"}}]}}]}", mapper);
        acc.consume("{\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,"
                + "\"function\":{\"arguments\":\"CostMs\\\":100}\"}}]}}]}", mapper);

        ChatResult result = acc.toResult();
        assertEquals(1, result.getToolCalls().size());
        ToolCall call = result.getToolCalls().get(0);
        assertEquals("call_1", call.getId());
        assertEquals("slow_sql", call.getName());
        assertEquals("{\"minCostMs\":100}", call.getArguments());
        assertNull(result.getContent(), "无正文增量时 content 应为 null");
    }

    /** 多个并行工具调用：按 index 分别累积，且保持顺序 */
    @Test
    void keepsMultipleToolCallsByIndex() {
        acc.consume("{\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c0\","
                + "\"function\":{\"name\":\"jvm_metrics\",\"arguments\":\"{}\"}}]}}]}", mapper);
        acc.consume("{\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":1,\"id\":\"c1\","
                + "\"function\":{\"name\":\"slow_sql\",\"arguments\":\"{}\"}}]}}]}", mapper);
        acc.consume("{\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,"
                + "\"function\":{\"arguments\":\"\"}}]}}]}", mapper);

        ChatResult result = acc.toResult();
        assertEquals(2, result.getToolCalls().size());
        assertEquals("jvm_metrics", result.getToolCalls().get(0).getName());
        assertEquals("slow_sql", result.getToolCalls().get(1).getName());
    }

    /** 无 arguments 分片的调用（部分网关只发 name）应兜底为 {} */
    @Test
    void defaultsArgumentsToEmptyObject() {
        acc.consume("{\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c0\","
                + "\"function\":{\"name\":\"generate_report\"}}]}}]}", mapper);

        assertEquals("{}", acc.toResult().getToolCalls().get(0).getArguments());
    }

    /** 非法 JSON / 空块静默忽略，不影响已累积内容 */
    @Test
    void ignoresMalformedChunks() {
        acc.consume("{\"choices\":[{\"delta\":{\"content\":\"有效\"}}]}", mapper);
        assertTrue(acc.consume("not-a-json", mapper).isEmpty());
        assertTrue(acc.consume("", mapper).isEmpty());
        assertTrue(acc.consume("{\"choices\":[]}", mapper).isEmpty());

        assertEquals("有效", acc.toResult().getContent());
    }

    /** 兼容顶层 delta（少数网关不返回 choices 数组） */
    @Test
    void supportsTopLevelDelta() {
        acc.consume("{\"delta\":{\"content\":\"兼容模式\"}}", mapper);

        assertEquals("兼容模式", acc.toResult().getContent());
    }
}
