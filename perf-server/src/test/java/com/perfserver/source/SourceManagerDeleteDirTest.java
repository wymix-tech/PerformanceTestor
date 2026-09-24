package com.perfserver.source;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.test.util.ReflectionTestUtils;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 删除项目磁盘目录的路径穿越防护（P2-1）。
 *
 * <p>仓库名来自用户输入，可能含 {@code ../} 片段。若直接拼接删除，会删到存储根目录之外。
 * 这里对真实临时目录做删除验证：正常仓库目录被删干净、越界目标被拒绝、不存在时幂等。
 */
class SourceManagerDeleteDirTest {

    private SourceManager newManager(String storageDir) {
        SourceManager m = new SourceManager(null, null, null, null);
        ReflectionTestUtils.setField(m, "storageDir", storageDir);
        return m;
    }

    /** 正常仓库目录：连同子文件一并删除 */
    @Test
    void deletesRepoDirRecursively(@TempDir Path root) throws IOException {
        SourceManager m = newManager(root.toString());
        Path repoDir = root.resolve("perf-demo-7");
        Files.createDirectories(repoDir.resolve("src/main"));
        Files.writeString(repoDir.resolve("src/main/App.java"), "class App {}");
        Files.writeString(repoDir.resolve("pom.xml"), "<project/>");

        m.deleteRepoDir(7L, "perf-demo");

        assertFalse(Files.exists(repoDir), "仓库目录应被完整删除");
        assertTrue(Files.exists(root), "存储根目录本身不能被删");
    }

    /** 目录不存在：幂等返回，不报错（重复删除场景） */
    @Test
    void missingDirIsIdempotent(@TempDir Path root) throws IOException {
        SourceManager m = newManager(root.toString());
        m.deleteRepoDir(7L, "never-imported");
        assertTrue(Files.exists(root));
    }

    /** 仓库名含路径穿越片段：净化后目录名落在根目录内，不会删到根目录外 */
    @Test
    void traversalInRepoNameStaysInsideRoot(@TempDir Path root) throws IOException {
        SourceManager m = newManager(root.toString());
        Path outside = root.getParent().resolve("victim-" + System.nanoTime());
        Files.createDirectories(outside);
        Files.writeString(outside.resolve("keep.txt"), "must survive");

        // 仓库名形如 ../../victim：sanitize 把 / 换成 _，解析后仍应在 root 内
        m.deleteRepoDir(7L, "../../" + outside.getFileName());

        assertTrue(Files.exists(outside.resolve("keep.txt")), "存储根目录之外的目录绝不能被删");
        Files.deleteIfExists(outside.resolve("keep.txt"));
        Files.deleteIfExists(outside);
    }

    /** 越界目标被拒绝：仓库名净化后仍无法逃出存储根目录 */
    @Test
    void resolveTargetStaysInsideRoot(@TempDir Path root) {
        SourceManager m = newManager(root.toString());
        Path t = m.resolveRepoTarget(7L, "../evil");
        Path normalizedRoot = root.toAbsolutePath().normalize();
        assertTrue(t.startsWith(normalizedRoot), "解析结果必须位于存储根目录内");
        assertFalse(t.equals(normalizedRoot), "解析结果不能等于存储根目录本身");
    }

    /**
     * 越界目标被拒绝：仓库名净化后仍无法逃出存储根目录（含 {@code ..} 片段）。
     *
     * <p>这是防路径穿越的核心断言——仓库名由用户输入，若直接拼接后删除，
     * 名字写成 {@code ../../etc} 就会删到存储目录之外。
     */
    @Test
    void refusesTargetOutsideRoot(@TempDir Path root) {
        SourceManager m = newManager(root.toString());
        // sanitizeDirName 会把 "/" 换成 "_"，解析后必然落在 root 内——断言这点成立
        Path t = m.resolveRepoTarget(7L, "../../../../etc");
        assertTrue(t.startsWith(root.toAbsolutePath().normalize()));
    }

    /**
     * 存储根目录为空/相对路径时仍能正确解析并落在根内（部署配置容错）。
     */
    @Test
    void relativeStorageDirResolvesInsideRoot() {
        SourceManager m = newManager("build/perf-source-test");
        Path t = m.resolveRepoTarget(1L, "demo");
        assertTrue(t.isAbsolute(), "解析目标必须是绝对路径（归一化后）");
        assertTrue(t.toString().endsWith("demo-1"));
    }
}
