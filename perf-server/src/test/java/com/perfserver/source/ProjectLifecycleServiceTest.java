package com.perfserver.source;

import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.service.DataCleanupService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.LinkedHashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
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
 * 删除项目编排（P2-1）：二次确认、进行中压测拦截、级联清理开关。
 *
 * <p>这几处都是"错了就丢数据"的分支：确认名不匹配必须拒绝、有压测在跑必须拒绝、
 * 未绑定应用时不能瞎清理（清理需要 appName 定位）。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class ProjectLifecycleServiceTest {

    @Mock
    private SourceManager sourceManager;

    @Mock
    private DataCleanupService dataCleanupService;

    @Mock
    private StressTaskMapper stressTaskMapper;

    private ProjectLifecycleService service;

    private SourceRepo repo;

    @BeforeEach
    void setUp() {
        service = new ProjectLifecycleService(sourceManager, dataCleanupService, stressTaskMapper);
        repo = new SourceRepo();
        repo.setId(7L);
        repo.setRepoName("perf-demo");
        repo.setAppName("perf-demo");
        repo.setFileCount(120);
        repo.setTotalSize(20480L);
        lenient().when(sourceManager.getRepo(7L)).thenReturn(repo);
        lenient().when(sourceManager.deleteRepo(7L)).thenReturn(120);
        lenient().when(stressTaskMapper.countRunningByApp("perf-demo")).thenReturn(0);
        Map<String, Integer> deleted = new LinkedHashMap<>();
        deleted.put("stressTask", 3);
        deleted.put("aiKnowledge", 2);
        lenient().when(dataCleanupService.cleanupByAppName(eq("perf-demo"), eq(false))).thenReturn(deleted);
    }

    /** 正常删除：确认名一致 → 清理历史数据 + 删源码，结果如实上报各删除量 */
    @Test
    void deleteWithMatchingNameCleansDataAndSource() {
        ProjectLifecycleService.DeleteResult r = service.deleteProject(7L, "perf-demo", true);

        assertEquals(7L, r.repoId);
        assertEquals("perf-demo", r.repoName);
        assertTrue(r.isCleanedData());
        assertEquals(120, r.sourceFiles);
        assertEquals(3, r.deleted.get("stressTask"));
        verify(dataCleanupService).cleanupByAppName("perf-demo", false);
        verify(sourceManager).deleteRepo(7L);
    }

    /** 二次确认名不匹配：拒绝删除，且不触碰任何数据（含首尾空白也不宽容） */
    @Test
    void mismatchedConfirmNameRejected() {
        assertThrows(IllegalArgumentException.class, () -> service.deleteProject(7L, "perf-dem", true));
        assertThrows(IllegalArgumentException.class, () -> service.deleteProject(7L, "PERF-DEMO", true));
        assertThrows(IllegalArgumentException.class, () -> service.deleteProject(7L, "", true));
        assertThrows(IllegalArgumentException.class, () -> service.deleteProject(7L, null, true));
        verify(sourceManager, never()).deleteRepo(anyLong());
        verify(dataCleanupService, never()).cleanupByAppName(anyString(), any(Boolean.class));
    }

    /** 确认名首尾空白应被容忍（用户复制粘贴常带空白），trim 后匹配即放行 */
    @Test
    void confirmNameIsTrimmedBeforeMatch() {
        ProjectLifecycleService.DeleteResult r = service.deleteProject(7L, "  perf-demo  ", true);
        assertEquals("perf-demo", r.repoName);
        verify(sourceManager).deleteRepo(7L);
    }

    /** 有进行中压测：拒绝删除（避免压测线程继续往已删项目写数据） */
    @Test
    void runningStressBlocksDelete() {
        when(stressTaskMapper.countRunningByApp("perf-demo")).thenReturn(2);

        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> service.deleteProject(7L, "perf-demo", true));
        assertTrue(e.getMessage().contains("进行中"));
        verify(sourceManager, never()).deleteRepo(anyLong());
        verify(dataCleanupService, never()).cleanupByAppName(anyString(), any(Boolean.class));
    }

    /** cleanData=false：只删项目自身，不碰历史数据 */
    @Test
    void cleanDataDisabledSkipsDataCleanup() {
        ProjectLifecycleService.DeleteResult r = service.deleteProject(7L, "perf-demo", false);

        assertFalse(r.isCleanedData());
        assertTrue(r.deleted.isEmpty());
        verify(dataCleanupService, never()).cleanupByAppName(anyString(), any(Boolean.class));
        verify(sourceManager).deleteRepo(7L);
    }

    /** 未绑定应用：无法定位历史数据，跳过清理（不报错、不瞎删） */
    @Test
    void unboundAppSkipsDataCleanup() {
        repo.setAppName("");
        ProjectLifecycleService.DeleteResult r = service.deleteProject(7L, "perf-demo", true);

        assertFalse(r.isCleanedData());
        verify(dataCleanupService, never()).cleanupByAppName(anyString(), any(Boolean.class));
        verify(sourceManager).deleteRepo(7L);
        // 未绑定应用时也不应做进行中压测校验（无 appName 可查）
        verify(stressTaskMapper, never()).countRunningByApp(anyString());
    }

    /** 删除预览：如实反映影响面（文件数/绑定状态/进行中压测数） */
    @Test
    void deletePreviewReportsImpact() {
        when(stressTaskMapper.countRunningByApp("perf-demo")).thenReturn(1);

        Map<String, Object> preview = service.deletePreview(7L);
        assertEquals(7L, preview.get("repoId"));
        assertEquals("perf-demo", preview.get("repoName"));
        assertEquals(120, preview.get("fileCount"));
        assertEquals(20480L, preview.get("totalSize"));
        assertEquals(true, preview.get("boundApp"));
        assertEquals(1, preview.get("runningStress"));
    }

    /** 项目不存在：直接抛错（由 SourceManager.getRepo 兜底） */
    @Test
    void missingRepoRejected() {
        when(sourceManager.getRepo(99L)).thenThrow(new IllegalArgumentException("仓库不存在: 99"));
        assertThrows(IllegalArgumentException.class, () -> service.deleteProject(99L, "x", true));
    }
}
