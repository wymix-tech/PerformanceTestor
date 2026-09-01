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
}
