package com.perfserver.collector;

import com.perfserver.mapper.SourceRepoMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 项目注册表：agent 上报准入校验（项目不存在 → 数据全部丢弃）。
 *
 * <p>项目名 = 源码仓库名（source_repo.repo_name）。
 * 启发式缓存 30s，避免每批上报都查库；缓存空集也算"有缓存"，防止未知项目穿透。
 *
 * <p><b>缓存失效</b>：缓存只 {@code put} 不 {@code evict} 会导致新建项目在 TTL 内
 * 上报被整批丢弃、改名的旧项目名仍被放行。故项目生命周期变更
 * （导入完成 / 重新同步 / 绑定应用 / 切换分支 / 删除）后必须显式调用
 * {@link #evict(String)} 失效对应条目。
 */
@Service
public class ProjectRegistry {

    private static final Logger log = LoggerFactory.getLogger(ProjectRegistry.class);

    private final SourceRepoMapper sourceRepoMapper;

    /** 项目名 → 是否存在（缓存 30s） */
    private final Map<String, CacheEntry> cache = new ConcurrentHashMap<>();
    private static final long CACHE_TTL_MS = 30_000;

    public ProjectRegistry(SourceRepoMapper sourceRepoMapper) {
        this.sourceRepoMapper = sourceRepoMapper;
    }

    /** 项目是否存在（带缓存） */
    public boolean exists(String projectName) {
        if (projectName == null || projectName.isBlank()) {
            return false;
        }
        long now = System.currentTimeMillis();
        CacheEntry entry = cache.get(projectName);
        if (entry != null && now - entry.time < CACHE_TTL_MS) {
            return entry.exists;
        }
        boolean exists = sourceRepoMapper.findByName(projectName) != null;
        cache.put(projectName, new CacheEntry(exists, now));
        return exists;
    }

    /**
     * 失效单个项目缓存：项目新建 / 改名 / 删除后调用。
     *
     * <p>调用后该项目名下次 {@link #exists(String)} 直接回查数据库，
     * 避免"刚导入的项目 30s 内上报被丢弃"与"已删除的项目仍在放行"。
     * 项目名为空时无操作（空名本就不参与缓存）。
     */
    public void evict(String projectName) {
        if (projectName == null || projectName.isBlank()) {
            return;
        }
        if (cache.remove(projectName) != null) {
            log.debug("project registry cache evicted: {}", projectName);
        }
    }

    /** 清空全部缓存：批量变更（如批量删除、配置重载）后调用 */
    public void evictAll() {
        int size = cache.size();
        cache.clear();
        if (size > 0) {
            log.info("project registry cache cleared: {} entries", size);
        }
    }

    /** 当前缓存条目数（供运维观测 / 单测断言） */
    int cacheSize() {
        return cache.size();
    }

    private record CacheEntry(boolean exists, long time) {
    }
}
