package com.perfserver.common.page;

import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 游标分页 SQL 的落库约束（静态回查，无需数据库）。
 *
 * <p>分页正确性全靠 SQL 里的三处细节，任何一处写错都会在真机上表现为
 * "偶发丢数据"，且单测的 mock 层看不出来：
 * <ol>
 *   <li>游标条件必须是 {@code (time < ? OR (time = ? AND id < ?))} 复合比较——
 *       只写 {@code time < ?} 会整批跳过时间并列的记录；</li>
 *   <li>排序键必须与游标条件一致（{@code ORDER BY time DESC, id DESC}）——
 *       排序键与游标不同会漏读/重读；</li>
 *   <li>语句必须全部参数化：游标值是外部输入，拼接即注入面。</li>
 * </ol>
 */
class CursorPagingSqlScriptTest {

    private static final String[] CURSOR_MAPPERS = {
            "/mapper/TraceChainMapper.xml",
            "/mapper/JvmMetricMapper.xml",
            "/mapper/SlowSqlMapper.xml",
            "/mapper/SlowCommandMapper.xml"};

    /** 时序表的分页排序键：表名 → (时间列, orderBy 片段) */
    private static final String[][] ORDER_KEYS = {
            {"TraceChainMapper", "start_time"},
            {"JvmMetricMapper", "collect_time"},
            {"SlowSqlMapper", "execute_time"},
            {"SlowCommandMapper", "execute_time"}};

    @Test
    void everyCursorQueryIsFullyParameterized() throws IOException {
        for (String resource : CURSOR_MAPPERS) {
            String xml = read(resource);
            assertFalse(xml.contains("${"), resource + " 不得使用 ${} 拼接（游标是外部输入）");
            assertTrue(xml.contains("#{cursorTime}"), resource + " 游标时间必须参数化");
            assertTrue(xml.contains("#{cursorId}"), resource + " 游标 id 必须参数化");
            assertTrue(xml.contains("#{limit}"), resource + " limit 必须参数化");
        }
    }

    /**
     * 复合游标条件：{@code (time < #{cursorTime} OR (time = #{cursorTime} AND id < #{cursorId}))}。
     *
     * <p>这是整个分页最容易被简化掉的一段——"时间已经唯一了，要 id 干嘛"的直觉
     * 在 DATETIME（秒精度）下是错的：同一秒内多条记录靠时间无法定序。
     */
    @Test
    void cursorConditionIsComposite() throws IOException {
        for (String[] entry : ORDER_KEYS) {
            String xml = read("/mapper/" + entry[0] + ".xml");
            String time = entry[1];
            assertTrue(xml.contains(time + " &lt; #{cursorTime}"),
                    entry[0] + " 缺少 cursorTime 下界比较");
            assertTrue(xml.contains("OR (" + time + " = #{cursorTime} AND id &lt; #{cursorId})"),
                    entry[0] + " 缺少时间并列时的 id 兜底比较（会整批跳过同秒记录）");
        }
    }

    /** 排序键必须与游标条件一致，且 id 参与定序 */
    @Test
    void orderByMatchesCursorKey() throws IOException {
        for (String[] entry : ORDER_KEYS) {
            String xml = read("/mapper/" + entry[0] + ".xml");
            assertTrue(xml.contains("ORDER BY " + entry[1] + " DESC, id DESC"),
                    entry[0] + " 分页排序必须为 (时间 DESC, id DESC)，否则游标翻页会漏/重");
        }
    }

    /** 分页语句只出现在 queryByCursor 中，不能污染图表用的正序 query */
    @Test
    void chartQueryKeepsAscendingOrder() throws IOException {
        String jvm = read("/mapper/JvmMetricMapper.xml");
        assertTrue(jvm.contains("ORDER BY collect_time ASC"),
                "图表数据源必须保持正序（时间从左到右），分页的倒序只用于 queryByCursor");
    }

    private static String read(String resource) throws IOException {
        try (InputStream in = CursorPagingSqlScriptTest.class.getResourceAsStream(resource)) {
            Assumptions.assumeTrue(in != null, "资源不存在，跳过： " + resource);
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }
}
