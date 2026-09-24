package com.perfserver.service;

import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 清理 / 保留策略的落库约束：
 * <ul>
 *   <li>数据清理 Mapper 禁止 {@code ${}} 拼接（全部走 #{} 参数化）；</li>
 *   <li>分区维护 Mapper 的 {@code ${}} 仅用于白名单表名/分区名，需同时存在 Java 侧校验；</li>
 *   <li>两份建表脚本必须同步包含分区表定义（否则新部署无分区可维护）。</li>
 * </ul>
 */
class RetentionSqlScriptTest {

    @Test
    void cleanupMapperIsFullyParameterized() throws IOException {
        String xml = readClasspath("/mapper/DataCleanupMapper.xml");

        assertFalse(xml.contains("${"), "清理 SQL 必须全部参数化，禁止 ${} 拼接");
        assertTrue(xml.contains("app_name = #{appName}"), "按应用清理需绑定 appName 参数");
        assertTrue(xml.contains("app_name = #{appName} AND app_name &lt;&gt; ''")
                        || xml.contains("app_name = #{appName} AND app_name <> ''"),
                "本应用 AI 结论清理必须显式排除 app_name='' 的全局结论");
        assertTrue(xml.contains("DELETE FROM ai_knowledge WHERE app_name = ''"),
                "全局结论删除需独立语句，仅由显式开关触发");
    }

    @Test
    void retentionMapperPlaceholdersAreWhitelistedIdentifiersOnly() throws IOException {
        String xml = readClasspath("/mapper/RetentionMapper.xml");

        // 标识符必须来自 Java 侧白名单常量，故 ${} 只允许出现在表名/列名/分区名位置
        assertTrue(xml.contains("${tableName}"), "分区/清理 DDL 需要表名拼接（DDL 不支持参数化标识符）");
        assertTrue(xml.contains("${partition}"), "分区名需拼接");
        assertTrue(xml.contains("#{lessThan}") || xml.contains("#{before}"), "时间边界必须参数化");
        // 越界值一律走 #{}：表名/分区名之外的任何值都不得拼进 SQL
        assertFalse(xml.contains("${appName}"), "应用名是外部输入，绝不可拼接");
        assertFalse(xml.contains("${before}"), "时间边界不可拼接");
        assertFalse(xml.contains("${batchSize}"), "批大小不可拼接");
    }

    @Test
    void bothInitScriptsDeclarePartitionedTables() throws IOException {
        for (String file : new String[]{"../sql/init.sql", "../sql/init_empty.sql"}) {
            String script = readScript(file);
            assertTrue(script.contains("PARTITION BY RANGE COLUMNS (collect_time)"),
                    file + " 需定义 jvm_metric 日分区（否则 PartitionMaintainer 无分区可维护）");
            assertTrue(script.contains("PARTITION BY RANGE COLUMNS (sample_time)"),
                    file + " 需定义 stress_sample 日分区");
            assertTrue(script.contains("PARTITION p_max VALUES LESS THAN (MAXVALUE)"),
                    file + " 需保留 p_max 兜底分区，避免缺分区导致写入失败");
        }
    }

    private static String readScript(String relative) throws IOException {
        Path path = Path.of(relative).toAbsolutePath().normalize();
        Assumptions.assumeTrue(Files.exists(path), "脚本不存在，跳过： " + path);
        return Files.readString(path, StandardCharsets.UTF_8);
    }

    private static String readClasspath(String resource) throws IOException {
        try (InputStream in = RetentionSqlScriptTest.class.getResourceAsStream(resource)) {
            Assumptions.assumeTrue(in != null, "资源不存在，跳过： " + resource);
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }
}
