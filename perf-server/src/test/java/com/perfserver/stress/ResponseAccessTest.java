package com.perfserver.stress;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 响应读取公共工具单测。
 *
 * <p>该工具类供结果判断与变量提取共用，其大小写敏感性与截断行为直接决定
 * 两者行为是否一致，故单独覆盖边界。
 */
class ResponseAccessTest {

    @Test
    void headerValueIsCaseInsensitive() {
        Map<String, List<String>> headers = Map.of("Content-Type", List.of("application/json"));
        assertEquals("application/json", ResponseAccess.headerValue(headers, "Content-Type"));
        assertEquals("application/json", ResponseAccess.headerValue(headers, "content-type"));
        assertEquals("application/json", ResponseAccess.headerValue(headers, "CONTENT-TYPE"));
    }

    @Test
    void headerValueReturnsNullWhenMissing() {
        Map<String, List<String>> headers = Map.of("Content-Type", List.of("application/json"));
        assertNull(ResponseAccess.headerValue(headers, "X-Token"));
        assertNull(ResponseAccess.headerValue(headers, null));
        assertNull(ResponseAccess.headerValue(headers, "  "));
        assertNull(ResponseAccess.headerValue(null, "X-Token"));
    }

    /** 同名多值头取第一个 */
    @Test
    void headerValueTakesFirstOfMultiValue() {
        Map<String, List<String>> headers = Map.of("Set-Cookie", List.of("a=1", "b=2"));
        assertEquals("a=1", ResponseAccess.headerValue(headers, "Set-Cookie"));
    }

    @Test
    void parseJsonTruncatesLongBody() {
        String body = "{\"data\":{\"v\":\"" + "x".repeat(200) + "\"}}";
        JsonNode root = ResponseAccess.parseJson(body, 64);
        assertNull(root, "截断后 JSON 不再完整，应解析失败返回 null");
    }

    @Test
    void parseJsonReturnsNullForNonJson() {
        assertNull(ResponseAccess.parseJson("<html>error</html>", 1024));
        assertNull(ResponseAccess.parseJson("", 1024));
        assertNull(ResponseAccess.parseJson(null, 1024));
    }

    @Test
    void resolvePathHandlesNestedAndMissing() {
        JsonNode root = ResponseAccess.parseJson("{\"data\":{\"token\":\"abc\"}}", 1024);
        JsonNode node = ResponseAccess.resolvePath(root, "data.token");
        assertEquals("abc", node.asText());
        assertNull(ResponseAccess.resolvePath(root, "data.nope"));
        assertNull(ResponseAccess.resolvePath(root, ""));
    }

    private static final String NESTED =
            "{\"data\":{\"token\":\"abc\",\"user\":{\"id\":42}},\"list\":[{\"id\":7},{\"id\":8}]}";

    /** 数组按下标取值（步骤上下文引用的 {{p1.result.list.0.id}} 依赖此能力） */
    @Test
    void resolvePathSupportsArrayIndex() {
        JsonNode root = ResponseAccess.parseJson(NESTED, 1024);
        assertEquals("7", ResponseAccess.resolvePath(root, "list.0.id").asText());
        assertEquals("8", ResponseAccess.resolvePath(root, "list.1.id").asText());
        // 数组节点本身（路径止于数组）
        assertTrue(ResponseAccess.resolvePath(root, "list").isArray());
    }

    /** 越界 / 负号 / 非数字段 / 对标量继续下钻 → null */
    @Test
    void resolvePathArrayIndexBoundaries() {
        JsonNode root = ResponseAccess.parseJson(NESTED, 1024);
        assertNull(ResponseAccess.resolvePath(root, "list.2.id"), "下标越界");
        assertNull(ResponseAccess.resolvePath(root, "list.99"));
        assertNull(ResponseAccess.resolvePath(root, "list.-1"), "负号不是合法下标");
        assertNull(ResponseAccess.resolvePath(root, "list.1x"));
        assertNull(ResponseAccess.resolvePath(root, "list.99999999999999999999"), "数字段溢出");
        assertNull(ResponseAccess.resolvePath(root, "data.token.x"), "标量节点无法继续下钻");
    }

    /**
     * ObjectNode 的 key 恰为数字串时仍按 key 取值——数组下标判断必须严格限定在
     * ArrayNode 分支内，否则会改变断言侧（共用本方法）的既有行为。
     */
    @Test
    void resolvePathKeepsNumericKeysOnObjectNodes() {
        JsonNode root = ResponseAccess.parseJson("{\"0\":\"zero\",\"list\":[{\"0\":\"in-array\"}]}", 1024);
        assertEquals("zero", ResponseAccess.resolvePath(root, "0").asText());
        assertEquals("in-array", ResponseAccess.resolvePath(root, "list.0.0").asText());
    }

    /** 深层嵌套（递归无深度限制） */
    @Test
    void resolvePathHandlesDeepNesting() {
        JsonNode root = ResponseAccess.parseJson("{\"a\":{\"b\":{\"c\":{\"d\":\"deep\"}}}}", 1024);
        assertEquals("deep", ResponseAccess.resolvePath(root, "a.b.c.d").asText());
        assertNull(ResponseAccess.resolvePath(root, "a.b.c.d.e"));
    }
}
