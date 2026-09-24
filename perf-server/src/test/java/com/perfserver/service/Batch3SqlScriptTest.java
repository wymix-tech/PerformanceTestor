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
 * 批次 3（P2）落库约束守卫：
 * <ul>
 *   <li>新增 Mapper（{@code StressTemplateMapper}）必须全参数化——模板名/应用名/JSON 都是外部输入；</li>
 *   <li>两份建表脚本必须同步包含 {@code stress_template} 表与 {@code fix_suggestion} 的回写列，
 *       且老库升级路径要有对应 {@code add_column_if_missing} 调用；</li>
 *   <li>删除项目不得出现 {@code ${}} 拼接（仓库名来自用户输入）。</li>
 * </ul>
 *
 * <p>这一类"只有真机/新老库对比才暴露"的语义，用静态回查先钉住，
 * 避免只在一份脚本里加表、老库升级时缺列却到运行时才炸。
 */
class Batch3SqlScriptTest {

    @Test
    void templateMapperIsFullyParameterized() throws IOException {
        String xml = readClasspath("/mapper/StressTemplateMapper.xml");

        assertFalse(xml.contains("${"), "压测模板 SQL 必须全部参数化，禁止 ${} 拼接");
        assertTrue(xml.contains("#{templateName}"), "模板名需绑定参数");
        assertTrue(xml.contains("#{appName}"), "应用名需绑定参数");
        assertTrue(xml.contains("#{taskJson}"), "模板参数 JSON 需绑定参数");
        assertTrue(xml.contains("app_name = #{appName} OR app_name = ''"),
                "按应用查模板需同时返回通用模板（与知识库同一语义）");
    }

    @Test
    void bothInitScriptsDeclareTemplateAndApplyColumns() throws IOException {
        for (String file : new String[]{"../sql/init.sql", "../sql/init_empty.sql"}) {
            String script = readScript(file);
            assertTrue(script.contains("CREATE TABLE IF NOT EXISTS stress_template"),
                    file + " 需定义 stress_template 表（否则模板功能在部署时缺失）");
            assertTrue(script.contains("KEY idx_app_time (app_name, create_time)"),
                    file + " 需为 stress_template 建应用维度索引");
            // fix_suggestion 回写列（新库直接建，老库靠 add_column_if_missing）
            assertTrue(script.contains("applied_at") && script.contains("apply_ref")
                            && script.contains("apply_type") && script.contains("verify_task_id"),
                    file + " 需包含 fix_suggestion 的修复回写列");
            assertTrue(script.contains("add_column_if_missing('fix_suggestion', 'applied_at'"),
                    file + " 老库升级需有 applied_at 补列调用（幂等）");
            assertTrue(script.contains("add_column_if_missing('fix_suggestion', 'verify_task_id'"),
                    file + " 老库升级需有 verify_task_id 补列调用（幂等）");
        }
    }

    @Test
    void twoInitScriptsStayInSyncForBatch3Tables() throws IOException {
        String init = readScript("../sql/init.sql");
        String empty = readScript("../sql/init_empty.sql");
        // 两份脚本的表定义必须一致（init_empty 只是不含演示数据）
        for (String marker : new String[]{
                "CREATE TABLE IF NOT EXISTS stress_template",
                "applied_at       DATETIME      NULL COMMENT '修复回写时间（NULL = 未回写）'",
                "CALL add_column_if_missing('fix_suggestion', 'apply_ref'"}) {
            assertTrue(init.contains(marker), "init.sql 缺少：" + marker);
            assertTrue(empty.contains(marker), "init_empty.sql 缺少：" + marker);
        }
    }

    private static String readScript(String relative) throws IOException {
        Path path = Path.of(relative).toAbsolutePath().normalize();
        Assumptions.assumeTrue(Files.exists(path), "脚本不存在，跳过： " + path);
        return Files.readString(path, StandardCharsets.UTF_8);
    }

    private static String readClasspath(String resource) throws IOException {
        try (InputStream in = Batch3SqlScriptTest.class.getResourceAsStream(resource)) {
            Assumptions.assumeTrue(in != null, "资源不存在，跳过： " + resource);
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }
}
