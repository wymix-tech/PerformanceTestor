package com.perfserver.health;

import com.perfserver.retention.PartitionMaintainer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;

/**
 * 平台自检：数据库连通与版本、必需表存在、源码目录可写、磁盘余量、分区表可用性。
 *
 * <p>两处复用同一套探针：
 * <ul>
 *   <li>{@link PlatformHealthIndicator} —— 运行期 {@code /actuator/health}；</li>
 *   <li>{@link StartupSelfCheck} —— 启动就绪时打印一次可操作结论。</li>
 * </ul>
 * 共用是为了让"启动时打印的告警"与"运行期看到的 DOWN"口径一致。
 */
@Service
public class HealthCheckService {

    private static final Logger log = LoggerFactory.getLogger(HealthCheckService.class);

    private final HealthMapper mapper;
    private final HealthProperties properties;

    public HealthCheckService(HealthMapper mapper, HealthProperties properties) {
        this.mapper = mapper;
        this.properties = properties;
    }

    /** 探针结果：{@code ok=false} 时 {@code message} 必须给出可操作的下一步 */
    public record Probe(boolean ok, String message) {
    }

    /** 数据库连通性与版本 */
    public Probe checkDatabase() {
        try {
            Integer pong = mapper.ping();
            if (pong == null || pong != 1) {
                return new Probe(false, "数据库探活返回异常结果，请检查平台库连接配置");
            }
        } catch (Exception e) {
            return new Probe(false, "数据库不可达: " + e.getMessage() + "（检查 MYSQL_URL / MYSQL_USER / MYSQL_PASSWORD）");
        }
        String version;
        try {
            version = mapper.databaseVersion();
        } catch (Exception e) {
            // 能 ping 通但取不到版本：多为非 MySQL 库或权限受限，降级为提示而非失败
            return new Probe(true, "数据库连通（版本未知: " + e.getMessage() + "）");
        }
        if (version == null) {
            return new Probe(true, "数据库连通（版本未知）");
        }
        if (isMysql(version) && compareVersion(mysqlVersionOf(version), properties.getMinMysqlVersion()) < 0) {
            return new Probe(false, "MySQL 版本 " + version + " 低于分区维护要求的最低版本 "
                    + properties.getMinMysqlVersion() + "，分区表管理语义不可靠");
        }
        return new Probe(true, "数据库连通: " + version);
    }

    /** 必需表存在性：缺失即说明初始化脚本未执行 */
    public Probe checkRequiredTables() {
        String[] required = properties.getRequiredTables();
        if (required == null || required.length == 0) {
            return new Probe(true, "未配置必需表清单，跳过检查");
        }
        List<String> tables = List.of(required);
        List<String> existing;
        try {
            existing = mapper.findExistingTables(tables);
        } catch (Exception e) {
            return new Probe(false, "表清单回查失败: " + e.getMessage());
        }
        List<String> missing = new ArrayList<>(tables);
        if (existing != null) {
            missing.removeAll(existing);
        }
        if (!missing.isEmpty()) {
            return new Probe(false, "缺少必需表 " + missing + "，请先执行 sql/init.sql（生产用 sql/init_empty.sql）");
        }
        return new Probe(true, "必需表齐全（" + tables.size() + " 张）");
    }

    /** 分区表可用性：建表脚本声明的分区表是否真的建了分区 */
    public Probe checkPartitioning() {
        List<String> tables = new ArrayList<>(PartitionMaintainer.PARTITIONED_TABLES.keySet());
        try {
            List<String> partitioned = mapper.findPartitionedTables(tables);
            List<String> missing = new ArrayList<>(tables);
            if (partitioned != null) {
                missing.removeAll(partitioned);
            }
            if (!missing.isEmpty()) {
                return new Probe(false, "分区表未生效: " + missing
                        + "（未分区时数据全落 p_max，分区退化为普通表，请按 sql/init.sql 重建分区）");
            }
            return new Probe(true, "分区表就绪: " + partitioned);
        } catch (Exception e) {
            return new Probe(false, "分区信息回查失败: " + e.getMessage());
        }
    }

    /** 源码存储目录可写 */
    public Probe checkStorageWritable(String dir) {
        if (dir == null || dir.isBlank()) {
            return new Probe(false, "源码存储目录未配置（perf.source.storage-dir）");
        }
        Path root = Paths.get(dir);
        try {
            Files.createDirectories(root);
            Path probe = Files.createTempFile(root, ".perf-health-", ".tmp");
            Files.deleteIfExists(probe);
            return new Probe(true, "源码存储目录可写: " + root.toAbsolutePath());
        } catch (IOException e) {
            return new Probe(false, "源码存储目录不可写: " + root.toAbsolutePath()
                    + "（" + e.getMessage() + "，检查挂载与运行用户权限）");
        }
    }

    /** 磁盘余量：低于阈值即视为不可用（写满会让导入与报告导出静默失败） */
    public Probe checkDisk(String dir) {
        String target = dir == null || dir.isBlank() ? "." : dir;
        try {
            Path root = Paths.get(target);
            Files.createDirectories(root);
            long freeMb = Files.getFileStore(root).getUsableSpace() / (1024 * 1024);
            if (freeMb < properties.getMinFreeDiskMb()) {
                return new Probe(false, "磁盘余量不足: 剩余 " + freeMb + "MB，低于阈值 "
                        + properties.getMinFreeDiskMb() + "MB");
            }
            return new Probe(true, "磁盘余量 " + freeMb + "MB（阈值 " + properties.getMinFreeDiskMb() + "MB）");
        } catch (IOException e) {
            return new Probe(false, "磁盘余量检查失败: " + e.getMessage());
        }
    }

    /** 从 {@code VERSION()} 结果中提取 MySQL 版本号（形如 {@code 8.0.36}） */
    static String mysqlVersionOf(String versionText) {
        if (versionText == null) {
            return "";
        }
        var matcher = java.util.regex.Pattern.compile("(\\d+\\.\\d+(?:\\.\\d+)?)").matcher(versionText);
        return matcher.find() ? matcher.group(1) : "";
    }

    static boolean isMysql(String versionText) {
        return versionText != null && versionText.toLowerCase(java.util.Locale.ROOT).contains("mysql")
                || (versionText != null && versionText.toLowerCase(java.util.Locale.ROOT).contains("mariadb"));
    }

    /**
     * 版本号数值比较：{@code a < b} 返回负值。
     *
     * <p>不能直接字符串比较——{@code "5.10" < "5.7"} 在字典序下成立但与语义相反。
     * 缺失的段按 0 补齐，非数字段（如 {@code -log}）截断。
     */
    static int compareVersion(String a, String b) {
        String[] as = a == null || a.isBlank() ? new String[0] : a.split("\\.");
        String[] bs = b == null || b.isBlank() ? new String[0] : b.split("\\.");
        for (int i = 0; i < Math.max(as.length, bs.length); i++) {
            int cmp = Integer.compare(segment(as, i), segment(bs, i));
            if (cmp != 0) {
                return cmp;
            }
        }
        return 0;
    }

    private static int segment(String[] parts, int index) {
        if (index >= parts.length) {
            return 0;
        }
        var matcher = java.util.regex.Pattern.compile("^(\\d+)").matcher(parts[index]);
        if (!matcher.find()) {
            return 0;
        }
        try {
            return Integer.parseInt(matcher.group(1));
        } catch (NumberFormatException e) {
            return 0;
        }
    }
}
