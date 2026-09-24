package com.perfserver.ai;

import com.perfserver.entity.AiKnowledge;
import com.perfserver.mapper.AiKnowledgeMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 知识库服务：结论入库门槛、检索降级与条数约束。
 */
class AiKnowledgeServiceTest {

    private AiKnowledgeMapper mapper;
    private AiKnowledgeService service;

    @BeforeEach
    void setUp() {
        mapper = mock(AiKnowledgeMapper.class);
        service = new AiKnowledgeService(mapper);
    }

    @Test
    void savesConclusion() {
        assertTrue(service.save("perf-demo", "订单列表慢 SQL 根因",
                "现象：压测 P95 偏高，慢 SQL 平均 1280ms；根因：t_order 缺少 (user_id,status) 复合索引导致全表扫描；"
                        + "修复：补充复合索引并将循环单查改写为联表查询。", "root_cause"));

        ArgumentCaptor<AiKnowledge> captor = ArgumentCaptor.forClass(AiKnowledge.class);
        verify(mapper).insert(captor.capture());
        AiKnowledge saved = captor.getValue();
        assertEquals("perf-demo", saved.getAppName());
        assertEquals("订单列表慢 SQL 根因", saved.getTitle());
        assertEquals("root_cause", saved.getTags());
        assertEquals("ai", saved.getSource());
        assertFalse(saved.getContent().isBlank());
    }

    @Test
    void skipsTooShortConclusion() {
        assertFalse(service.save("app", "短", "太短", "tag"));
        assertFalse(service.save("app", "空", null, "tag"));
        verify(mapper, never()).insert(org.mockito.ArgumentMatchers.any());
    }

    @Test
    void fallsBackToTitleFromContentFirstLine() {
        service.save("app", "   ", "首行作为标题\n第二行是正文内容，用于满足最短入库长度门槛，避免被判定为无信息量而丢弃。", "root_cause");

        ArgumentCaptor<AiKnowledge> captor = ArgumentCaptor.forClass(AiKnowledge.class);
        verify(mapper).insert(captor.capture());
        assertEquals("首行作为标题", captor.getValue().getTitle());
        assertEquals("app", captor.getValue().getAppName());
    }

    @Test
    void clampsSearchLimit() {
        when(mapper.search(anyString(), anyString(), anyInt())).thenReturn(List.of());

        service.search("慢 SQL", null, 1000);

        verify(mapper).search(eq("慢 SQL"), isNull(), eq(AiKnowledgeService.MAX_LIMIT));
    }

    @Test
    void blankQueryListsRecent() {
        when(mapper.recent(anyString(), anyInt())).thenReturn(List.of());

        service.search("  ", "perf-demo", 10);

        verify(mapper).recent("perf-demo", 10);
        verify(mapper, never()).search(anyString(), anyString(), anyInt());
    }

    /** FULLTEXT 不可用（未建索引/兼容库）时降级为最近结论，保证对话链路不中断 */
    @Test
    void degradesToRecentWhenFulltextFails() {
        when(mapper.search(anyString(), anyString(), anyInt())).thenThrow(new RuntimeException("fulltext not supported"));
        when(mapper.recent(anyString(), anyInt())).thenReturn(List.of(new AiKnowledge()));

        assertEquals(1, service.search("慢 SQL", "app", 5).size());
        verify(mapper).recent("app", 5);
    }

    /**
     * 检索无命中必须返回空：不能回落 recent 把无关结论当"参考经验"喂给模型
     * （与提示词"工具返回空 = 暂无数据"一致，见方案文档 §二·5 的语义冲突裁决）。
     */
    @Test
    void returnsEmptyWhenNoHit() {
        when(mapper.search(anyString(), anyString(), anyInt())).thenReturn(List.of());

        assertEquals(0, service.search("冷门关键词", "app", 8).size());
        verify(mapper, never()).recent(anyString(), anyInt());
    }

    /** mapper.search 返回 null（极端情况）同样按"无命中"处理，不回落 recent */
    @Test
    void returnsEmptyWhenHitsNull() {
        when(mapper.search(anyString(), anyString(), anyInt())).thenReturn(null);

        assertEquals(0, service.search("关键词", "app", 5).size());
        verify(mapper, never()).recent(anyString(), anyInt());
    }

    /** 有命中时如实返回，不额外拼接最近结论 */
    @Test
    void returnsHitsWithoutAppendingRecent() {
        AiKnowledge hit = new AiKnowledge();
        hit.setTitle("命中结论");
        when(mapper.search(anyString(), anyString(), anyInt())).thenReturn(List.of(hit));

        List<AiKnowledge> result = service.search("慢 SQL", "app", 5);

        assertEquals(1, result.size());
        assertEquals("命中结论", result.get(0).getTitle());
        verify(mapper, never()).recent(anyString(), anyInt());
    }

    @Test
    void deleteSwallowsException() {
        doThrow(new RuntimeException("db down")).when(mapper).deleteById(9L);

        assertFalse(service.delete(9L));
    }

    // ==================== 沉淀去重 ====================

    /** 同一问题反复对话产出近似结论：第二次不再入库，避免重复项淹没检索结果 */
    @Test
    void skipsDuplicateConclusion() {
        String content = "现象：压测 P95 偏高，慢 SQL 平均 1280ms；根因：t_order 缺少 (user_id,status)"
                + " 复合索引导致全表扫描；修复：补充复合索引并将循环单查改写为联表查询。";
        when(mapper.recent(anyString(), anyInt())).thenReturn(List.of());
        assertTrue(service.save("app", "订单列表慢 SQL 根因", content, "root_cause"));

        // 第二次：内容仅有一处措辞差异，应判定为重复
        when(mapper.recent(eq("app"), anyInt())).thenReturn(List.of(knowledge(content)));
        assertFalse(service.save("app", "订单列表慢 SQL 根因", content + "建议尽快落地。", "root_cause"));

        verify(mapper, org.mockito.Mockito.times(1)).insert(org.mockito.ArgumentMatchers.any());
    }

    /** 话题不同则不算重复，不能误伤正常沉淀 */
    @Test
    void savesDifferentConclusion() {
        when(mapper.recent(anyString(), anyInt())).thenReturn(List.of(
                knowledge("慢 SQL 根因：t_order 缺少复合索引导致全表扫描，建议补充索引并改写联表查询。")));

        assertTrue(service.save("app", "JVM 调优建议",
                "现象：Full GC 每分钟 6 次；根因：堆内存偏小且存在大对象缓存；建议扩堆并改为软引用缓存。",
                "tuning"));
    }

    /** 回看窗口查询异常时放行写入：宁可多存一条，也不能阻塞沉淀链路 */
    @Test
    void savesWhenDedupLookupFails() {
        when(mapper.recent(anyString(), anyInt())).thenThrow(new RuntimeException("db down"));

        assertTrue(service.save("app", "慢 SQL 根因",
                "现象：压测 P95 偏高；根因：缺少复合索引导致全表扫描；建议补充索引并改写联表查询。", "root_cause"));
        verify(mapper).insert(org.mockito.ArgumentMatchers.any());
    }

    @Test
    void similarityOfSameTextIsOne() {
        assertEquals(1.0, AiKnowledgeService.similarity("慢 SQL 根因缺少索引", "慢 SQL 根因缺少索引"), 1e-9);
    }

    @Test
    void similarityOfUnrelatedTextIsLow() {
        double s = AiKnowledgeService.similarity("缺少复合索引导致全表扫描",
                "Full GC 频繁且堆内存不足需扩容");
        assertTrue(s < AiKnowledgeService.DEDUP_SIMILARITY, "无关文本相似度应低于阈值，实际 " + s);
    }

    /** 空白与长度差异不影响判定（同一结论的换行/缩进差异很常见） */
    @Test
    void similarityIgnoresWhitespace() {
        assertEquals(1.0, AiKnowledgeService.similarity("慢 SQL 根因 缺少索引", "慢SQL根因缺少索引"), 1e-9);
    }

    private static AiKnowledge knowledge(String content) {
        AiKnowledge k = new AiKnowledge();
        k.setAppName("app");
        k.setContent(content);
        return k;
    }
}
