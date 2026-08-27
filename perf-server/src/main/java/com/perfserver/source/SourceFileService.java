package com.perfserver.source;

import com.perfserver.entity.SourceFile;
import com.perfserver.mapper.SourceFileMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * 源码文件索引与读取：扫描磁盘目录建立 source_file 索引，语言识别，按需懒加载文件内容。
 */
@Service
public class SourceFileService {

    private static final Logger log = LoggerFactory.getLogger(SourceFileService.class);

    /** 语言识别：扩展名 -> 语言类型 */
    private static final Map<String, String> LANGUAGE_MAP = new HashMap<>();
    /** 文本文件扩展名白名单 */
    private static final Set<String> TEXT_EXTS = Set.of(
            "java", "xml", "yml", "yaml", "json", "properties", "sql", "md", "txt",
            "html", "js", "ts", "vue", "css", "scss", "less", "kt", "gradle", "conf", "ini", "sh", "bat");

    static {
        LANGUAGE_MAP.put("java", "java");
        LANGUAGE_MAP.put("xml", "xml");
        LANGUAGE_MAP.put("yml", "yaml");
        LANGUAGE_MAP.put("yaml", "yaml");
        LANGUAGE_MAP.put("json", "json");
        LANGUAGE_MAP.put("properties", "properties");
        LANGUAGE_MAP.put("sql", "sql");
        LANGUAGE_MAP.put("md", "markdown");
        LANGUAGE_MAP.put("html", "html");
        LANGUAGE_MAP.put("js", "javascript");
        LANGUAGE_MAP.put("ts", "typescript");
        LANGUAGE_MAP.put("vue", "vue");
        LANGUAGE_MAP.put("css", "css");
        LANGUAGE_MAP.put("kt", "kotlin");
        LANGUAGE_MAP.put("gradle", "groovy");
        LANGUAGE_MAP.put("sh", "shell");
    }

    private final SourceFileMapper sourceFileMapper;

    @Value("${perf.source.max-file-size-mb:1}")
    private long maxFileSizeMb;
    @Value("${perf.source.max-total-size-mb:200}")
    private long maxTotalSizeMb;
    @Value("${perf.source.max-file-count:5000}")
    private int maxFileCount;

    public SourceFileService(SourceFileMapper sourceFileMapper) {
        this.sourceFileMapper = sourceFileMapper;
    }

    /** 识别文件语言类型 */
    public String detectLanguage(String fileName) {
        int dot = fileName.lastIndexOf('.');
        if (dot < 0) {
            return "text";
        }
        String ext = fileName.substring(dot + 1).toLowerCase(Locale.ROOT);
        return LANGUAGE_MAP.getOrDefault(ext, TEXT_EXTS.contains(ext) ? ext : "binary");
    }

    /**
     * 扫描磁盘目录并批量建索引（幂等：已存在的相对路径跳过）。
     *
     * @return 新索引文件数
     */
    public int indexDirectory(long repoId, Path rootDir) throws IOException {
        if (!Files.isDirectory(rootDir)) {
            throw new IllegalArgumentException("源码目录不存在: " + rootDir);
        }
        List<SourceFile> batch = new ArrayList<>();
        int indexed = 0;
        int fileCount = 0;
        long totalSize = 0;
        for (Path file : walkFiles(rootDir)) {
            Path relative = rootDir.relativize(file);
            String rel = relative.toString().replace('\\', '/');
            if (isIgnored(rel)) {
                continue;
            }
            long size = Files.size(file);
            fileCount++;
            totalSize += size;
            if (size > maxFileSizeMb * 1024 * 1024) {
                log.warn("跳过超大文件 {} ({} bytes)", rel, size);
                continue;
            }
            String language = detectLanguage(file.getFileName().toString());
            if ("binary".equals(language)) {
                continue;
            }
            if (sourceFileMapper.findByRepoAndPath(repoId, rel) != null) {
                continue; // 幂等跳过
            }
            SourceFile sf = new SourceFile();
            sf.setRepoId(repoId);
            sf.setRelativePath(rel);
            sf.setFileName(file.getFileName().toString());
            sf.setLanguage(language);
            sf.setSize(size);
            sf.setContentHash(hashFile(file));
            batch.add(sf);
            if (batch.size() >= 500) {
                sourceFileMapper.batchInsert(batch);
                indexed += batch.size();
                batch.clear();
            }
        }
        if (!batch.isEmpty()) {
            sourceFileMapper.batchInsert(batch);
            indexed += batch.size();
        }
        if (fileCount > maxFileCount) {
            throw new IllegalArgumentException("文件数超过上限 " + maxFileCount);
        }
        if (totalSize > maxTotalSizeMb * 1024 * 1024) {
            throw new IllegalArgumentException("源码总大小超过上限 " + maxTotalSizeMb + "MB");
        }
        log.info("index directory {} done, indexed={}, files={}, size={}", rootDir, indexed, fileCount, totalSize);
        return indexed;
    }

    /** 递归列出全部文件（目录深度限制 20，避免符号链接死循环） */
    private List<Path> walkFiles(Path root) throws IOException {
        List<Path> result = new ArrayList<>();
        try (var stream = Files.walk(root, 20)) {
            stream.filter(Files::isRegularFile).forEach(result::add);
        }
        return result;
    }

    /** 忽略 git 元数据与构建产物 */
    private boolean isIgnored(String rel) {
        return rel.startsWith(".git/") || rel.startsWith(".idea/")
                || rel.endsWith(".class") || rel.contains("/target/")
                || rel.contains("/node_modules/") || rel.contains("/dist/");
    }

    private String hashFile(Path file) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(Files.readAllBytes(file));
            StringBuilder sb = new StringBuilder();
            for (byte b : digest) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    /** 读取文件内容（懒加载，单文件上限内） */
    public String readContent(Path rootDir, SourceFile sf) throws IOException {
        Path file = rootDir.resolve(sf.getRelativePath()).normalize();
        if (!file.startsWith(rootDir.normalize())) {
            throw new IllegalArgumentException("非法文件路径: " + sf.getRelativePath());
        }
        if (Files.size(file) > maxFileSizeMb * 1024 * 1024) {
            return "// 文件过大，未加载";
        }
        return Files.readString(file);
    }

    /** 读取指定行区间（1-based，含两端） */
    public String readLines(Path rootDir, SourceFile sf, int lineStart, int lineEnd) throws IOException {
        String content = readContent(rootDir, sf);
        String[] lines = content.split("\n", -1);
        int from = Math.max(0, lineStart - 1);
        int to = Math.min(lines.length, lineEnd);
        if (from >= to) {
            return "";
        }
        StringBuilder sb = new StringBuilder();
        for (int i = from; i < to; i++) {
            sb.append(i + 1).append(": ").append(lines[i]).append("\n");
        }
        return sb.toString();
    }
}
