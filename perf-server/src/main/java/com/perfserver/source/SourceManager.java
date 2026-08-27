package com.perfserver.source;

import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.SourceRepoMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * 源码仓库导入：支持 zip 上传（防路径穿越解压）、git clone、本地目录导入。
 * 安全校验：解压路径强制位于目标目录内；文件大小/总大小/文件数上限。
 */
@Component
public class SourceManager {

    private static final Logger log = LoggerFactory.getLogger(SourceManager.class);
    private static final long MAX_SINGLE_FILE = 1024L * 1024;      // 1MB
    private static final long MAX_TOTAL_SIZE = 200L * 1024 * 1024; // 200MB
    private static final int MAX_FILE_COUNT = 5000;

    private final SourceRepoMapper sourceRepoMapper;
    private final SourceFileService sourceFileService;
    private final ExecutorService importExecutor = Executors.newFixedThreadPool(2, r -> {
        Thread t = new Thread(r, "source-import");
        t.setDaemon(true);
        return t;
    });

    @Value("${perf.source.storage-dir:/data/perf-source}")
    private String storageDir;

    public SourceManager(SourceRepoMapper sourceRepoMapper, SourceFileService sourceFileService) {
        this.sourceRepoMapper = sourceRepoMapper;
        this.sourceFileService = sourceFileService;
    }

    /**
     * 创建仓库记录并异步导入。
     *
     * @param repoName 仓库名
     * @param repoType zip / git / local
     * @param payload  zip 文件路径（zip）/ git URL（git）/ 本地目录（local）
     * @return 仓库记录（含 id）
     */
    public SourceRepo startImport(String repoName, String repoType, String payload) {
        if (repoName == null || repoName.isBlank()) {
            throw new IllegalArgumentException("仓库名称不能为空");
        }
        String type = repoType == null ? "zip" : repoType.toLowerCase(Locale.ROOT);
        if (!type.equals("zip") && !type.equals("git") && !type.equals("local")) {
            throw new IllegalArgumentException("不支持的导入类型: " + type);
        }
        SourceRepo repo = new SourceRepo();
        repo.setRepoName(repoName.trim());
        repo.setRepoType(type);
        repo.setSourcePath(payload);
        repo.setStatus("importing");
        sourceRepoMapper.insert(repo);
        long repoId = repo.getId();

        importExecutor.submit(() -> {
            try {
                doImport(repoId, type, payload);
            } catch (Exception e) {
                log.error("source import failed, repoId={}", repoId, e);
                sourceRepoMapper.updateStatus(repoId, "failed", e.getMessage() == null ? e.toString() : e.getMessage());
            }
        });
        return repo;
    }

    private void doImport(long repoId, String type, String payload) throws IOException {
        Path rootDir = repoRootDir(repoId);
        Files.createDirectories(rootDir);
        switch (type) {
            case "zip" -> unzipSafe(Paths.get(payload), rootDir);
            case "git" -> gitClone(payload, rootDir);
            case "local" -> copyLocalDir(Paths.get(payload), rootDir);
            default -> throw new IllegalArgumentException("不支持的导入类型: " + type);
        }
        int indexed = sourceFileService.indexDirectory(repoId, rootDir);
        long totalSize = sizeOf(rootDir);
        sourceRepoMapper.updateIndexed(repoId, indexed, totalSize);
        log.info("source repo {} imported, indexed={}", repoId, indexed);
    }

    /** 仓库磁盘根目录 */
    private Path repoRootDir(long repoId) {
        return Paths.get(storageDir).resolve("repo-" + repoId);
    }

    private long sizeOf(Path dir) throws IOException {
        try (var stream = Files.walk(dir, 20)) {
            return stream.filter(Files::isRegularFile).mapToLong(p -> {
                try {
                    return Files.size(p);
                } catch (IOException e) {
                    return 0L;
                }
            }).sum();
        }
    }

    /** 保存上传的 zip 到临时目录，返回磁盘路径 */
    public String saveZip(InputStream in, String originalName) throws IOException {
        Path uploadDir = Paths.get(storageDir).resolve("uploads");
        Files.createDirectories(uploadDir);
        String safeName = originalName == null ? "upload.zip" : Paths.get(originalName).getFileName().toString();
        Path target = uploadDir.resolve(UUID.randomUUID() + "-" + safeName);
        try (OutputStream out = Files.newOutputStream(target)) {
            in.transferTo(out);
        }
        return target.toString();
    }

    /** zip 解压（防路径穿越：解压路径强制位于目标目录内） */
    void unzipSafe(Path zipPath, Path destDir) throws IOException {
        if (!Files.exists(zipPath)) {
            throw new IllegalArgumentException("zip 文件不存在: " + zipPath);
        }
        Path normalizedDest = destDir.toAbsolutePath().normalize();
        long total = 0;
        int count = 0;
        try (ZipFile zipFile = new ZipFile(zipPath.toFile())) {
            var entries = zipFile.entries();
            while (entries.hasMoreElements()) {
                ZipEntry entry = entries.nextElement();
                if (entry.isDirectory()) {
                    continue;
                }
                String name = entry.getName();
                if (name.contains("..") || name.startsWith("/") || name.startsWith("\\")) {
                    throw new IllegalArgumentException("zip 包含非法路径，已中止解压: " + name);
                }
                Path target = normalizedDest.resolve(name).normalize();
                if (!target.startsWith(normalizedDest)) {
                    throw new IllegalArgumentException("zip 路径越界，已中止解压: " + name);
                }
                Path parent = target.getParent();
                if (parent != null) {
                    Files.createDirectories(parent);
                }
                try (InputStream in = zipFile.getInputStream(entry)) {
                    Files.copy(in, target, StandardCopyOption.REPLACE_EXISTING);
                }
                long size = Files.size(target);
                total += size;
                count++;
                if (size > MAX_SINGLE_FILE) {
                    throw new IllegalArgumentException("单文件超过 1MB 上限: " + name);
                }
                if (total > MAX_TOTAL_SIZE) {
                    throw new IllegalArgumentException("源码总大小超过 200MB 上限");
                }
                if (count > MAX_FILE_COUNT) {
                    throw new IllegalArgumentException("文件数超过 5000 上限");
                }
            }
        }
    }

    /** git clone（浅克隆，限制深度 1） */
    private void gitClone(String url, Path destDir) {
        if (url == null || url.isBlank() || (!url.startsWith("http://") && !url.startsWith("https://"))) {
            throw new IllegalArgumentException("git 地址必须为 http/https 协议");
        }
        try {
            ProcessBuilder pb = new ProcessBuilder("git", "clone", "--depth", "1", url, destDir.toString());
            pb.redirectErrorStream(true);
            Process process = pb.start();
            try (var reader = process.getInputStream()) {
                reader.transferTo(OutputStream.nullOutputStream());
            }
            int exit = process.waitFor();
            if (exit != 0) {
                throw new IllegalStateException("git clone 失败，退出码: " + exit);
            }
        } catch (IOException e) {
            throw new IllegalStateException("git 命令执行失败，请确认服务器已安装 git", e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("git clone 被中断", e);
        }
    }

    /** 本地目录导入：复制到平台存储目录（保证隔离与快照） */
    private void copyLocalDir(Path source, Path destDir) throws IOException {
        if (!Files.isDirectory(source)) {
            throw new IllegalArgumentException("本地目录不存在: " + source);
        }
        Path normalizedDest = destDir.toAbsolutePath().normalize();
        try (var stream = Files.walk(source, 20)) {
            for (Path p : stream.filter(Files::isRegularFile).toList()) {
                Path relative = source.relativize(p);
                String rel = relative.toString().replace('\\', '/');
                if (rel.startsWith(".git/") || rel.contains("/target/") || rel.contains("/node_modules/")) {
                    continue;
                }
                Path target = normalizedDest.resolve(relative).normalize();
                if (!target.startsWith(normalizedDest)) {
                    throw new IllegalArgumentException("本地路径越界: " + rel);
                }
                Files.createDirectories(target.getParent());
                Files.copy(p, target, StandardCopyOption.REPLACE_EXISTING);
            }
        }
    }

    /** 查询仓库列表 */
    public java.util.List<SourceRepo> listRepos(int limit) {
        return sourceRepoMapper.query(Math.max(1, Math.min(limit, 100)));
    }

    public SourceRepo getRepo(long repoId) {
        SourceRepo repo = sourceRepoMapper.findById(repoId);
        if (repo == null) {
            throw new IllegalArgumentException("仓库不存在: " + repoId);
        }
        return repo;
    }

    public Path getRepoRoot(SourceRepo repo) {
        return repoRootDir(repo.getId());
    }
}
