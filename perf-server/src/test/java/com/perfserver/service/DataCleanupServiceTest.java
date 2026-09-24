package com.perfserver.service;

import com.perfserver.mapper.DataCleanupMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 项目历史数据清理：全局 AI 结论默认保留、开关开启才删、空应用名拒绝执行。
 *
 * <p>对应方案文档 §二·5 的验收标准（ISSUE #14）。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class DataCleanupServiceTest {

    @Mock
    private DataCleanupMapper mapper;

    private DataCleanupService service;

    @BeforeEach
    void setUp() {
        service = new DataCleanupService(mapper);
        when(mapper.findStressTaskIdsByApp(anyString())).thenReturn(List.of());
        when(mapper.findIssueIdsByApp(anyString())).thenReturn(List.of());
        when(mapper.deleteAiKnowledgeByApp(anyString())).thenReturn(3);
        when(mapper.deleteGlobalAiKnowledge()).thenReturn(1);
    }

    /** 默认清理：只删本应用结论，全局结论（app_name=''）一条不动 */
    @Test
    void defaultCleanupKeepsGlobalKnowledge() {
        Map<String, Integer> deleted = service.cleanupByAppName("perf-demo");

        verify(mapper).deleteAiKnowledgeByApp("perf-demo");
        verify(mapper, never()).deleteGlobalAiKnowledge();
        assertEquals(3, deleted.get("aiKnowledge"));
        // 未开启时恒为 0：前端据此判断"全局结论未被动过"
        assertEquals(0, deleted.get("aiKnowledgeGlobal"));
    }

    /** 显式开启：追加删除全局结论，计数拆到独立键 */
    @Test
    void includeGlobalDeletesGlobalKnowledge() {
        Map<String, Integer> deleted = service.cleanupByAppName("perf-demo", true);

        verify(mapper).deleteGlobalAiKnowledge();
        assertEquals(3, deleted.get("aiKnowledge"));
        assertEquals(1, deleted.get("aiKnowledgeGlobal"));
    }

    /** 空应用名：直接拒绝，绝不允许无过滤删除 */
    @Test
    void blankAppNameRejected() {
        assertThrows(IllegalArgumentException.class, () -> service.cleanupByAppName(""));
        assertThrows(IllegalArgumentException.class, () -> service.cleanupByAppName("   "));
        assertThrows(IllegalArgumentException.class, () -> service.cleanupByAppName(null));
        verify(mapper, never()).deleteAiKnowledgeByApp(anyString());
        verify(mapper, never()).deleteGlobalAiKnowledge();
        verify(mapper, never()).deleteTraceByApp(anyString());
    }

    /** 应用名首尾空白应被规范化后再匹配，避免" app "查不到数据 */
    @Test
    void appNameTrimmed() {
        service.cleanupByAppName("  perf-demo  ");
        verify(mapper).deleteAiKnowledgeByApp("perf-demo");
    }

    /** 清理覆盖表清单：新增/删除清理表时必须显式更新本断言，防静默漏表 */
    @Test
    void cleanupCoversExpectedTables() {
        Map<String, Integer> deleted = service.cleanupByAppName("app");

        for (String key : List.of("stressSample", "stressTask", "fixSuggestion", "performanceIssue",
                "traceChain", "jvmMetric", "slowSql", "slowCommand", "appInstance", "dbConn",
                "aiKnowledge", "aiKnowledgeGlobal")) {
            assertTrue(deleted.containsKey(key), "清理结果缺少计数键: " + key);
        }
        // chat_message 无 app_name 列，补列前不纳入清理（见方案文档 P0-3）
        verify(mapper, never()).deleteStressSamplesByTaskIds(any());
    }
}
