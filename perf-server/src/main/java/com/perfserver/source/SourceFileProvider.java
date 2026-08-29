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
            List<SourceFile> indexed = fileMapper.listByRepo(repoId, null, 200);
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
}

