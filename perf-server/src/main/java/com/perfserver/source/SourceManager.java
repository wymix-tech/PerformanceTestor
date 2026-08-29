package com.perfserver.source;

import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.SourceFileMapper;
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
    private final SourceFileMapper sourceFileMapper;
    private final ExecutorService importExecutor = Executors.newFixedThreadPool(2, r -> {
        Thread t = new Thread(r, "source-import");
        t.setDaemon(true);
        return t;
    });

    @Value("${perf.source.storage-dir:/data/perf-source}")
    private String storageDir;

    public SourceManager(SourceRepoMapper sourceRepoMapper, SourceFileService sourceFileService,
                         SourceFileMapper sourceFileMapper) {
        this.sourceRepoMapper = sourceRepoMapper;
        this.sourceFileService = sourceFileService;
        this.sourceFileMapper = sourceFileMapper;
    }

    /**
     * 创建仓库记录并异步导入。
     *
     * @param repoName 仓库名
     * @param repoType zip / git / local
     * @param payload  zip 文件路径（zip）/ git URL（git）/ 本地目录（local）
     * @return 仓库记录（含 id）
     */
    public SourceRepo startImport(String repoName, String repoType, String payload, String branch,
                                  String gitUser, String gitToken, String appName) {
        String type = repoType == null ? "zip" : repoType.toLowerCase(Locale.ROOT);
        if (!type.equals("zip") && !type.equals("git") && !type.equals("local")) {
            throw new IllegalArgumentException("不支持的导入类型: " + type);
        }
        if (payload == null || payload.isBlank()) {
            throw new IllegalArgumentException("导入来源不能为空（zip 路径 / git 地址 / 本地目录）");
        }
        // 名称未填时自动派生：git 取 URL 末段、local 取路径末段（zip 由上传侧按文件名传入）
        String name = repoName == null || repoName.isBlank()
                ? deriveRepoName(type, payload) : repoName.trim();
        if (name.isBlank()) {
            throw new IllegalArgumentException("仓库名称不能为空");
        }
        SourceRepo repo = new SourceRepo();
        repo.setRepoName(name);
        repo.setRepoType(type);
        repo.setSourcePath(payload);
        repo.setBranch(branch == null ? "" : branch.trim());
        // 应用名 = 项目（仓库）名，agent 上报 appName 应与项目名一致
        repo.setAppName(appName == null || appName.isBlank() ? name : appName.trim());
        repo.setGitUser(gitUser == null ? "" : gitUser.trim());
        repo.setGitToken(gitToken == null ? "" : gitToken.trim());
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
        SourceRepo repo = sourceRepoMapper.findById(repoId);
        switch (type) {
            case "zip" -> unzipSafe(Paths.get(payload), rootDir);
            case "git" -> gitClone(payload, rootDir, repo.getBranch(), repo.getGitUser(), repo.getGitToken());
            case "local" -> copyLocalDir(Paths.get(payload), rootDir);
            default -> throw new IllegalArgumentException("不支持的导入类型: " + type);
        }
        int indexed = sourceFileService.indexDirectory(repoId, rootDir);
        long totalSize = sizeOf(rootDir);
        sourceRepoMapper.updateIndexed(repoId, indexed, totalSize);
        log.info("source repo {} imported, indexed={}", repoId, indexed);
    }

    /**
     * 重新同步仓库（同步执行）：清空平台副本 → 按原类型重新拉取 → 重建文件索引。
     * 用于源码更新后刷新接口扫描、源码关联等所有依赖仓库内容的功能。
     *
     * @return 重新索引的文件数
     */
    public int resync(long repoId) {
        SourceRepo repo = getRepo(repoId);
        if ("importing".equals(repo.getStatus())) {
            throw new IllegalStateException("仓库正在导入中，请稍后再试");
        }
        String type = repo.getRepoType() == null ? "local" : repo.getRepoType().toLowerCase(Locale.ROOT);
        String payload = repo.getSourcePath();
        if (payload == null || payload.isBlank()) {
            throw new IllegalArgumentException("仓库缺少来源信息，无法重新同步");
        }
        try {
            Path rootDir = repoRootDir(repoId);
            if ("git".equals(type)) {
                // git 来源：副本保留 .git，fetch 指定分支 → checkout/重置到远端最新（支持分支切换）
                String authUrl = buildAuthUrl(payload, repo.getGitUser(), repo.getGitToken());
                Files.createDirectories(rootDir);
                if (!Files.exists(rootDir.resolve(".git"))) {
                    gitClone(payload, rootDir, repo.getBranch(), repo.getGitUser(), repo.getGitToken());
                } else {
                    String branch = repo.getBranch() == null ? "" : repo.getBranch().trim();
                    // 1. fetch 全部分支引用（显式 refspec；凭据注入 URL，origin remote 保持干净）
                    runGit(java.util.List.of("git", "-C", rootDir.toString(), "fetch", "--depth", "1",
                            authUrl, "+refs/heads/*:refs/remotes/origin/*"), "git fetch 失败");
                    if (!branch.isEmpty()) {
                        // 2. 切换/重置到远端分支最新（-B 强制，分支与上次不同也可）
                        runGit(java.util.List.of("git", "-C", rootDir.toString(), "checkout", "-B",
                                branch, "origin/" + branch), "git checkout 失败");
                    }
                    // 3. 清理工作区（忽略本地改动，与远端严格一致）
                    runGit(java.util.List.of("git", "-C", rootDir.toString(), "reset", "--hard"), "git reset 失败");
                    runGit(java.util.List.of("git", "-C", rootDir.toString(), "clean", "-fd"), "git clean 失败");
                }
            } else {
                // zip / local 来源：清空副本后重新拉取（保持快照语义）
                if (Files.exists(rootDir)) {
                    try (var stream = Files.walk(rootDir)) {
                        stream.sorted(java.util.Comparator.reverseOrder())
                                .forEach(p -> {
                                    try {
                                        Files.deleteIfExists(p);
                                    } catch (IOException ignored) {
                                    }
                                });
                    }
                }
                Files.createDirectories(rootDir);
                if ("zip".equals(type)) {
                    unzipSafe(Paths.get(payload), rootDir);
                } else {
                    copyLocalDir(Paths.get(payload), rootDir);
                }
            }
            // 全量重建索引（增/删/改都同步）
            sourceFileMapper.deleteByRepo(repoId);
            int indexed = sourceFileService.indexDirectory(repoId, rootDir);
            long totalSize = sizeOf(rootDir);
            sourceRepoMapper.updateIndexed(repoId, indexed, totalSize);
            sourceRepoMapper.updateStatus(repoId, "ready", null);
            log.info("source repo {} resynced ({}), indexed={}", repoId, type, indexed);
            return indexed;
        } catch (Exception e) {
            sourceRepoMapper.updateStatus(repoId, "failed", e.getMessage() == null ? e.toString() : e.getMessage());
            if (e instanceof RuntimeException re) {
                throw re;
            }
            throw new IllegalStateException("重新同步失败: " + e.getMessage(), e);
        }
    }

    /** 仓库磁盘根目录：<存储目录>/<项目名>-<id>（目录名可读，一眼辨认项目；同名靠 id 区分） */
    private Path repoRootDir(long repoId) {
        SourceRepo repo = sourceRepoMapper.findById(repoId);
        String name = repo == null || repo.getRepoName() == null ? "repo" : repo.getRepoName();
        return Paths.get(storageDir).resolve(sanitizeDirName(name) + "-" + repoId);
    }

    /** 目录名净化：仅保留中英文/数字/._-，其余替换下划线 */
    static String sanitizeDirName(String name) {
        String s = name.replaceAll("[^A-Za-z0-9._\\-\u4e00-\u9fa5]", "_");
        return s.isBlank() ? "repo" : s;
    }

    /** 名称自动派生：git 取 URL 末段（去 .git）、local 取路径末段 */
    static String deriveRepoName(String type, String payload) {
        String p = payload.replaceAll("[/\\\\]+$", "");
        if ("git".equals(type)) {
            p = p.substring(p.lastIndexOf('/') + 1);
            if (p.endsWith(".git")) {
                p = p.substring(0, p.length() - 4);
            }
            return p;
        }
        if ("local".equals(type)) {
            return p.substring(Math.max(0, p.lastIndexOf('/') + 1));
        }
        String fileName = Paths.get(p).getFileName().toString();
        return fileName.endsWith(".zip") ? fileName.substring(0, fileName.length() - 4) : fileName;
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

    /**
     * 构造带凭据的 git URL：仅 http/https 注入 userinfo（URL 编码），file:// 原样返回。
     * 组合规则：user+token → user:token@；仅 token → token@；仅 user → user@。
     * 注意：返回值含明文凭据，仅用于 git 命令参数，禁止落库与打日志。
     */
    private String buildAuthUrl(String url, String gitUser, String gitToken) {
        if (url == null || !url.startsWith("http")) {
            return url;
        }
        String user = gitUser == null ? "" : gitUser.trim();
        String token = gitToken == null ? "" : gitToken.trim();
        if (user.isEmpty() && token.isEmpty()) {
            return url;
        }
        int schemeEnd = url.indexOf("://");
        String rest = url.substring(schemeEnd + 3);
        String userinfo = (user.isEmpty() ? "" : java.net.URLEncoder.encode(user, java.nio.charset.StandardCharsets.UTF_8))
                + (token.isEmpty() ? "" : ":" + java.net.URLEncoder.encode(token, java.nio.charset.StandardCharsets.UTF_8));
        return url.substring(0, schemeEnd + 3) + userinfo + "@" + rest;
    }

    /**
     * git clone（浅克隆深度 1，可指定分支；branch 空 = 远端默认分支）。
     * 不使用 --single-branch：--depth 1 已限制体积，保留全分支远端引用可供分支列表与切换使用；
     * 远端 HEAD 悬空（默认分支名不一致，如 master vs main）时回退检出 main 或 master。
     */
    private void gitClone(String url, Path destDir, String branch, String gitUser, String gitToken) {
        if (url == null || url.isBlank()
                || !(url.startsWith("http://") || url.startsWith("https://") || url.startsWith("file://"))) {
            // 放行 file://（与本地目录导入同级风险）；禁止 ssh://、git:// 等需凭据/非预期协议
            throw new IllegalArgumentException("git 地址仅支持 http/https/file 协议");
        }
        java.util.List<String> cmd = new java.util.ArrayList<>(java.util.List.of(
                "git", "clone", "--depth", "1", "--no-single-branch"));
        if (branch != null && !branch.isBlank()) {
            cmd.add("--branch");
            cmd.add(branch.trim());
        }
        cmd.add(buildAuthUrl(url, gitUser, gitToken));
        cmd.add(destDir.toString());
        runGit(cmd, "git clone 失败");
        if (branch == null || branch.isBlank()) {
            checkoutFallbackIfEmpty(destDir);
        }
    }

    /** clone 后工作区为空（远端 HEAD 悬空）时，依次尝试检出 origin/main、origin/master */
    private void checkoutFallbackIfEmpty(Path destDir) {
        boolean empty = true;
        try (var stream = Files.list(destDir)) {
            empty = stream.filter(p -> !p.getFileName().toString().equals(".git")).findAny().isEmpty();
        } catch (IOException ignored) {
            return;
        }
        if (!empty) {
            return;
        }
        for (String b : java.util.List.of("main", "master")) {
            try {
                runGit(java.util.List.of("git", "-C", destDir.toString(), "checkout", "-B", b, "origin/" + b),
                        "checkout " + b + " 失败");
                return;
            } catch (Exception ignored) {
                // 尝试下一个默认分支名
            }
        }
    }

    /** 执行 git 命令：非 0 退出抛业务异常，输出丢弃 */
    private void runGit(java.util.List<String> cmd, String failMsg) {
        try {
            ProcessBuilder pb = new ProcessBuilder(cmd);
            pb.redirectErrorStream(true);
            Process process = pb.start();
            try (var reader = process.getInputStream()) {
                reader.transferTo(OutputStream.nullOutputStream());
            }
            int exit = process.waitFor();
            if (exit != 0) {
                throw new IllegalStateException(failMsg + "（退出码 " + exit + "），请检查地址/分支/网络及服务器 git 凭据");
            }
        } catch (IOException e) {
            throw new IllegalStateException("git 命令执行失败，请确认服务器已安装 git", e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("git 操作被中断", e);
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

    /**
     * 枚举远端分支（无需 clone）：git ls-remote --heads 直连远端，
     * 用于 git 导入表单的下拉选择。支持私有仓库凭据注入。30s 超时防挂起。
     */
    public java.util.List<String> listRemoteBranches(String url, String gitUser, String gitToken) {
        if (url == null || url.isBlank()
                || !(url.startsWith("http://") || url.startsWith("https://") || url.startsWith("file://"))) {
            throw new IllegalArgumentException("git 地址仅支持 http/https/file 协议");
        }
        try {
            ProcessBuilder pb = new ProcessBuilder("git", "ls-remote", "--heads",
                    buildAuthUrl(url.trim(), gitUser, gitToken));
            pb.redirectErrorStream(true);
            Process process = pb.start();
            String out;
            try (var reader = process.getInputStream()) {
                out = new String(reader.readAllBytes());
            }
            if (!process.waitFor(30, java.util.concurrent.TimeUnit.SECONDS)) {
                process.destroyForcibly();
                throw new IllegalStateException("获取分支超时，请检查地址与网络");
            }
            if (process.exitValue() != 0) {
                throw new IllegalStateException("无法访问远端仓库，请检查地址/网络/凭据");
            }
            java.util.List<String> branches = new java.util.ArrayList<>();
            for (String line : out.split("\n")) {
                int idx = line.indexOf("refs/heads/");
                if (idx >= 0) {
                    branches.add(line.substring(idx + "refs/heads/".length()).trim());
                }
            }
            java.util.Collections.sort(branches);
            return branches;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("获取分支被中断", e);
        } catch (IllegalStateException e) {
            throw e;
        } catch (Exception e) {
            throw new IllegalStateException("获取分支列表失败: " + e.getMessage(), e);
        }
    }

    /**
     * 拉取仓库分支列表：以远端为准（ls-remote 直连枚举），与导入表单同一数据源，
     * 不受本地副本残留引用影响。git 类型专用。
     */
    public java.util.List<String> listBranches(long repoId) {
        SourceRepo repo = getRepo(repoId);
        if (!"git".equals(repo.getRepoType() == null ? "" : repo.getRepoType().toLowerCase(Locale.ROOT))) {
            throw new IllegalArgumentException("仅 git 仓库支持分支列表");
        }
        return listRemoteBranches(repo.getSourcePath(), repo.getGitUser(), repo.getGitToken());
    }

    /**
     * 绑定应用：绑定后压测/问题/源码分析自动归属该应用；空串 = 解绑。
     *
     * @return 绑定的应用名
     */
    public String bindApp(long repoId, String appName) {
        getRepo(repoId);
        sourceRepoMapper.updateAppName(repoId, appName == null ? "" : appName.trim());
        return appName == null ? "" : appName.trim();
    }

    /**
     * 切换分支：校验目标分支存在于远端 → 更新仓库 branch 配置 → resync。
     * 校验防止非法分支名污染配置（如误传 'origin'）。
     *
     * @return 切换后索引的文件数
     */
    public int switchBranch(long repoId, String branch) {
        if (branch == null || branch.isBlank()) {
            throw new IllegalArgumentException("目标分支不能为空");
        }
        SourceRepo repo = getRepo(repoId);
        if (!"git".equals(repo.getRepoType() == null ? "" : repo.getRepoType().toLowerCase(Locale.ROOT))) {
            throw new IllegalArgumentException("仅 git 仓库支持切换分支");
        }
        String target = branch.trim();
        java.util.List<String> remote = listRemoteBranches(repo.getSourcePath(), repo.getGitUser(), repo.getGitToken());
        if (!remote.contains(target)) {
            throw new IllegalArgumentException("远端不存在分支: " + target + "，可用分支: " + remote);
        }
        sourceRepoMapper.updateBranch(repoId, target);
        return resync(repoId);
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
