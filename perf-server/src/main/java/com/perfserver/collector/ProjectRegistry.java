package com.perfserver.collector;

import com.perfserver.mapper.SourceRepoMapper;
import org.springframework.stereotype.Service;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 项目注册表：agent 上报准入校验（项目不存在 → 数据全部丢弃）。
 *
 * <p>项目即应用：项目名 = 源码仓库名（source_repo.repo_name）。
 * 启发式缓存 30s，避免每批上报都查库；缓存空集也算"有缓存"，防止未知项目穿透。
 */
@Service
public class ProjectRegistry {

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

    private record CacheEntry(boolean exists, long time) {
    }
}
