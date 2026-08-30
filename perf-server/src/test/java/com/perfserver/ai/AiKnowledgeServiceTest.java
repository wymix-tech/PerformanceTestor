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

    /** 检索无命中时给出最近结论作参考，避免模型误判"无历史经验" */
    @Test
    void returnsRecentWhenNoHit() {
        when(mapper.search(anyString(), anyString(), anyInt())).thenReturn(List.of());
        when(mapper.recent(anyString(), anyInt())).thenReturn(List.of(new AiKnowledge(), new AiKnowledge()));

        assertEquals(2, service.search("冷门关键词", "app", 8).size());
        verify(mapper).recent("app", 5);
    }

    @Test
    void deleteSwallowsException() {
        doThrow(new RuntimeException("db down")).when(mapper).deleteById(9L);

        assertFalse(service.delete(9L));
    }
}
