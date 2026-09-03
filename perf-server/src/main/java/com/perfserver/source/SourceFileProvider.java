package com.perfserver.source;

import com.perfserver.entity.SourceFile;
import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.SourceFileMapper;
import com.perfserver.mapper.SourceRepoMapper;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 宿主源码索引 → 引擎模块文件供给适配。
 * 从 source_repo/source_file 索引按需读取 .java/.xml 内容，喂给 MyBatis 源码解析器。
 * 目录不可读时返回空列表（引擎降级为纯 DB 元数据模式）。
 */
@Component
public class SourceFileProvider {

    /** 灌数扫描的文件上限（引擎做魔法值/关联扫描，抽样即可） */
    public static final int GEN_SCAN_LIMIT = 200;
    /**
     * 按接口分析的文件上限。
     *
     * <p>调用链需跨文件解析类型（Controller → Service → 实现 → Mapper），
     * 200 的抽样上限在大仓库上会直接漏掉整条链路，故分析场景放宽。</p>
     */
    public static final int ANALYSIS_LIMIT = 5000;

    private final SourceRepoMapper repoMapper;
    private final SourceFileMapper fileMapper;
    private final SourceFileService sourceFileService;

    public SourceFileProvider(SourceRepoMapper repoMapper, SourceFileMapper fileMapper,
                              SourceFileService sourceFileService) {
        this.repoMapper = repoMapper;
        this.fileMapper = fileMapper;
        this.sourceFileService = sourceFileService;
    }

    /**
     * 加载仓库源码文件（仅 .java/.xml，上限 200 个避免大仓库阻塞）。
     * 仓库不存在或磁盘路径不可读时返回空列表。
     */
    public List<com.perftestor.datagen.model.SourceFile> loadFiles(long repoId) {
        return loadFiles(repoId, GEN_SCAN_LIMIT);
    }

    /**
     * 加载仓库源码文件（仅 .java/.xml，最多 {@code limit} 个）。
     *
     * @param repoId 仓库 ID
     * @param limit  文件数上限；灌数扫描用 {@value #GEN_SCAN_LIMIT}，按接口分析用
     *               {@value #ANALYSIS_LIMIT}（调用链需跨文件解析类型，截断会漏表）
     * @return 可读的源码文件；仓库不存在或路径不可读时返回空列表
     */
    public List<com.perftestor.datagen.model.SourceFile> loadFiles(long repoId, int limit) {
        List<com.perftestor.datagen.model.SourceFile> out = new ArrayList<>();
        SourceRepo repo = repoMapper.findById(repoId);
        if (repo == null || repo.getSourcePath() == null || repo.getSourcePath().isBlank()) {
            return out;
        }
        Path root = Paths.get(repo.getSourcePath());
        if (!Files.isDirectory(root)) {
            return out;
        }
        try {
            List<SourceFile> indexed = fileMapper.listByRepo(repoId, null, limit);
            for (SourceFile sf : indexed) {
                String name = sf.getFileName() == null ? "" : sf.getFileName().toLowerCase(Locale.ROOT);
                if (!name.endsWith(".java") && !name.endsWith(".xml")) {
                    continue;
                }
                try {
                    String content = sourceFileService.readContent(root, sf);
                    if (content != null && !content.isBlank()
                            && !content.startsWith("// 文件过大")) {
                        out.add(new com.perftestor.datagen.model.SourceFile(sf.getRelativePath(), content));
                    }
                } catch (IOException e) {
                    // 单文件读取失败跳过
                }
            }
        } catch (Exception e) {
            // 索引查询失败：返回已加载部分（可能为空）
        }
        return out;
    }

    /**
     * 仓库已索引文件总数（用于判定加载是否被上限截断）。
     *
     * <p>截断必须被显式感知：调用链分析若只看到部分源码，会漏掉 Service/Mapper 而
     * 表现为「接口分析不出表」，用户无从判断是接口不查库还是源码没读全。</p>
     */
    public int countIndexed(long repoId) {
        try {
            return fileMapper.countByRepo(repoId);
        } catch (Exception e) {
            return 0;
        }
    }
}

