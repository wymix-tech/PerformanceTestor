package com.perfserver.ai;

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
 * 知识库落库约束：建表脚本双份同步 + FULLTEXT ngram 中文分词 + Mapper 禁止 ${} 拼接。
 */
class AiKnowledgeSqlScriptTest {

    private static final String TABLE = "ai_knowledge";

    @Test
    void bothInitScriptsCreateFulltextTable() throws IOException {
        String init = readScript("../sql/init.sql");
        String initEmpty = readScript("../sql/init_empty.sql");

        for (String script : new String[]{init, initEmpty}) {
            assertTrue(script.contains("CREATE TABLE IF NOT EXISTS " + TABLE),
                    "建表语句需幂等（两份脚本同步）");
            assertTrue(script.contains("FULLTEXT KEY"), "需建全文索引支撑中文检索");
            assertTrue(script.contains("WITH PARSER ngram"), "需显式指定 ngram 解析器以支持中文分词");
            assertTrue(script.contains("MATCH") || script.contains("ft_content"),
                    "需声明全文索引列");
        }
    }

    @Test
    void mapperUsesFulltextMatchAndNoStringInterpolation() throws IOException {
        String xml = readClasspath("/mapper/AiKnowledgeMapper.xml");

        assertTrue(xml.contains("MATCH(title, content) AGAINST"), "检索需走 FULLTEXT MATCH ... AGAINST");
        assertTrue(xml.contains("IN NATURAL LANGUAGE MODE"), "检索需为自然语言模式");
        assertFalse(xml.contains("${"), "Mapper XML 禁止 ${} 拼接（SQL 注入风险）");
    }

    private static String readScript(String relative) throws IOException {
        Path path = Path.of(relative).toAbsolutePath().normalize();
        Assumptions.assumeTrue(Files.exists(path), "脚本不存在，跳过： " + path);
        return Files.readString(path, StandardCharsets.UTF_8);
    }

    private static String readClasspath(String resource) throws IOException {
        try (InputStream in = AiKnowledgeSqlScriptTest.class.getResourceAsStream(resource)) {
            Assumptions.assumeTrue(in != null, "资源不存在，跳过： " + resource);
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }
}
