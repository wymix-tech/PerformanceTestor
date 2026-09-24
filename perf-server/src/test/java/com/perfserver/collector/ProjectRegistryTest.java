package com.perfserver.collector;

import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.SourceRepoMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 项目注册表：缓存命中/失效语义。
 *
 * <p>关键回归点：新建项目后 30s 内上报不得被整批丢弃，
 * 删除项目后不得继续放行（原实现只 put 无 evict）。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class ProjectRegistryTest {

    @Mock
    private SourceRepoMapper sourceRepoMapper;

    private ProjectRegistry registry;

    @BeforeEach
    void setUp() {
        registry = new ProjectRegistry(sourceRepoMapper);
    }

    @Test
    void cachesLookupWithinTtl() {
        when(sourceRepoMapper.findByName("demo")).thenReturn(new SourceRepo());

        assertTrue(registry.exists("demo"));
        assertTrue(registry.exists("demo"));
        // 第二次走缓存，不再查库
        verify(sourceRepoMapper, times(1)).findByName("demo");
    }

    @Test
    void cachesNotFoundToPreventPenetration() {
        when(sourceRepoMapper.findByName("ghost")).thenReturn(null);

        assertFalse(registry.exists("ghost"));
        assertFalse(registry.exists("ghost"));
        verify(sourceRepoMapper, times(1)).findByName("ghost");
    }

    /** 新建项目后 evict：下一次查询立即回查数据库，不再吃旧缓存（否则 TTL 内上报被丢弃） */
    @Test
    void evictRefreshesUnknownProject() {
        when(sourceRepoMapper.findByName("new-app")).thenReturn(null);
        assertFalse(registry.exists("new-app"));

        // 项目刚导入：库里已有记录，同时失效缓存
        when(sourceRepoMapper.findByName("new-app")).thenReturn(new SourceRepo());
        registry.evict("new-app");

        assertTrue(registry.exists("new-app"));
    }

    /** 删除项目后 evict：不再按旧缓存放行上报 */
    @Test
    void evictRevokesDeletedProject() {
        when(sourceRepoMapper.findByName("gone")).thenReturn(new SourceRepo());
        assertTrue(registry.exists("gone"));

        when(sourceRepoMapper.findByName("gone")).thenReturn(null);
        registry.evict("gone");

        assertFalse(registry.exists("gone"));
    }

    @Test
    void evictAllClearsEverything() {
        when(sourceRepoMapper.findByName("a")).thenReturn(new SourceRepo());
        when(sourceRepoMapper.findByName("b")).thenReturn(null);
        registry.exists("a");
        registry.exists("b");
        assertEquals(2, registry.cacheSize());

        registry.evictAll();

        assertEquals(0, registry.cacheSize());
    }

    /** 空/空白项目名：既不查库也不进缓存，直接判不存在 */
    @Test
    void blankNameNeverCached() {
        assertFalse(registry.exists(null));
        assertFalse(registry.exists("  "));
        assertEquals(0, registry.cacheSize());
    }

    @Test
    void evictBlankIsNoop() {
        registry.evict(null);
        registry.evict("  ");
        assertEquals(0, registry.cacheSize());
    }
}
