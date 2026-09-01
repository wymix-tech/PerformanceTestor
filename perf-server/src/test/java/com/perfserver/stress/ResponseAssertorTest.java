package com.perfserver.stress;

import com.perfserver.stress.StressTaskRequest.Assertion;
import com.perfserver.stress.StressTaskRequest.VarExtract;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 结果判断（断言）单测。
 *
 * <p>核心场景：HTTP 200 但业务失败（如 {@code {"code":500}}）必须被判为错误，
 * 否则压测错误率会严重失真——这是引入结果判断的根本原因。
 */
class ResponseAssertorTest {

    private static Assertion a(String source, String key, String op, String expected) {
        Assertion x = new Assertion();
        x.setSource(source);
        x.setKey(key);
        x.setOp(op);
        x.setExpected(expected);
        return x;
    }

    private static Map<String, List<String>> hdr(String... kv) {
        Map<String, List<String>> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put(kv[i], List.of(kv[i + 1]));
        }
        return m;
    }

    private static final String BIZ_OK = "{\"code\":0,\"data\":{\"orderId\":1}}";
    private static final String BIZ_FAIL = "{\"code\":500,\"message\":\"库存不足\"}";

    @Test
    void matchesStatusCode() {
        assertTrue(ResponseAssertor.matches(List.of(a("status", null, "eq", "200")), 200, null, null));
        assertFalse(ResponseAssertor.matches(List.of(a("status", null, "eq", "200")), 500, null, null));
        assertTrue(ResponseAssertor.matches(List.of(a("status", null, "lt", "400")), 200, null, null));
        assertTrue(ResponseAssertor.matches(List.of(a("status", null, "gt", "199")), 200, null, null));
    }

    /** 无断言规则时全部通过（保持历史行为：仅按状态码判定） */
    @Test
    void matchesWithoutRules() {
        assertTrue(ResponseAssertor.matches(null, 500, null, null));
        assertTrue(ResponseAssertor.matches(List.of(), 500, null, null));
    }

    /** 200 包裹业务失败：json 断言必须识别——本特性的核心诉求 */
    @Test
    void detectsBusinessFailureBehindHttp200() {
        List<Assertion> rules = List.of(a("json", "code", "eq", "0"));
        assertTrue(ResponseAssertor.matches(rules, 200, null, BIZ_OK));
        assertFalse(ResponseAssertor.matches(rules, 200, null, BIZ_FAIL));
    }

    @Test
    void matchesBodyAndJson() {
        assertTrue(ResponseAssertor.matches(List.of(a("body", null, "contains", "success")),
                200, null, "{\"result\":\"success\"}"));
        assertFalse(ResponseAssertor.matches(List.of(a("body", null, "contains", "success")),
                200, null, "{\"result\":\"failed\"}"));
        // 路径缺失：默认判失败，但 ne / notContains 语义下应判通过
        assertFalse(ResponseAssertor.matches(List.of(a("json", "nope.x", "eq", "1")), 200, null, BIZ_OK));
        assertTrue(ResponseAssertor.matches(List.of(a("json", "nope.x", "ne", "1")), 200, null, BIZ_OK));
    }

    @Test
    void matchesResponseHeaderCaseInsensitive() {
        Map<String, List<String>> headers = hdr("Content-Type", "application/json");
        assertTrue(ResponseAssertor.matches(List.of(a("header", "Content-Type", "contains", "json")),
                200, headers, null));
        assertTrue(ResponseAssertor.matches(List.of(a("header", "content-type", "contains", "json")),
                200, headers, null));
        // 头缺失：默认判失败，notContains / ne 判通过
        assertFalse(ResponseAssertor.matches(List.of(a("header", "X-Token", "contains", "abc")),
                200, headers, null));
        assertTrue(ResponseAssertor.matches(List.of(a("header", "X-Token", "notContains", "abc")),
                200, headers, null));
    }

    /** 多规则按 AND 生效 */
    @Test
    void allRulesMustPass() {
        List<Assertion> rules = List.of(
                a("status", null, "eq", "200"),
                a("json", "code", "eq", "0"));
        assertTrue(ResponseAssertor.matches(rules, 200, null, BIZ_OK));
        assertFalse(ResponseAssertor.matches(rules, 200, null, BIZ_FAIL));
        assertFalse(ResponseAssertor.matches(rules, 500, null, BIZ_OK));
    }

    @Test
    void supportsRegexAndNumericCompare() {
        assertTrue(ResponseAssertor.matches(List.of(a("body", null, "regex", "\"code\":\\d+")), 200, null, BIZ_OK));
        assertTrue(ResponseAssertor.matches(List.of(a("json", "code", "lt", "1")), 200, null, BIZ_OK));
        assertFalse(ResponseAssertor.matches(List.of(a("json", "code", "lt", "1")), 200, null, BIZ_FAIL));
        // 非法正则不应抛异常，按不命中处理
        assertFalse(ResponseAssertor.matches(List.of(a("body", null, "regex", "[invalid")), 200, null, BIZ_OK));
    }

    /** 正则预编译缓存：结果必须与即时编译一致（含跨规则复用与非法正则缓存） */
    @Test
    void regexCacheKeepsResultsConsistent() {
        List<Assertion> rules = List.of(a("body", null, "regex", "\"code\":0"));
        for (int i = 0; i < 1000; i++) {
            assertTrue(ResponseAssertor.matches(rules, 200, null, BIZ_OK));
            assertFalse(ResponseAssertor.matches(rules, 200, null, BIZ_FAIL));
        }
        // 非法正则被缓存后，重复调用仍应稳定返回「不命中」而非抛异常
        List<Assertion> bad = List.of(a("body", null, "regex", "[invalid"));
        for (int i = 0; i < 100; i++) {
            assertFalse(ResponseAssertor.matches(bad, 200, null, BIZ_OK));
        }
    }

    /** 断言中连续空格应被精确保留（前端解析与后端比较共同保证） */
    @Test
    void preservesConsecutiveSpacesInExpected() {
        assertTrue(ResponseAssertor.matches(List.of(a("body", null, "contains", "a  b")), 200, null, "xa  by"));
        assertFalse(ResponseAssertor.matches(List.of(a("body", null, "contains", "a b")), 200, null, "xa  by"));
    }

    /** 未知来源/操作符应容错，不因配置笔误让整轮压测全判失败 */
    @Test
    void toleratesUnknownSourceAndOp() {
        assertTrue(ResponseAssertor.matches(List.of(a("unknown", "x", "eq", "1")), 200, null, BIZ_OK));
        assertTrue(ResponseAssertor.matches(List.of(a("status", null, "bogusOp", "200")), 200, null, null));
    }

    /** needsBody：纯 status/header 断言不读响应体（压测热路径的性能优化） */
    @Test
    void needsBodyOnlyForBodyOrJson() {
        assertFalse(ResponseAssertor.needsBody(List.of(a("status", null, "eq", "200"))));
        assertFalse(ResponseAssertor.needsBody(List.of(a("header", "X-Token", "contains", "a"))));
        assertTrue(ResponseAssertor.needsBody(List.of(a("json", "code", "eq", "0"))));
        assertTrue(ResponseAssertor.needsBody(List.of(a("body", null, "contains", "ok"))));
        assertFalse(ResponseAssertor.needsBody(null));
    }

    @Test
    void compareHandlesNullAndNumericStrings() {
        assertTrue(ResponseAssertor.compare("eq", "200", "200"));
        assertTrue(ResponseAssertor.compare("eq", "200", "200.0"));
        assertTrue(ResponseAssertor.compare("ne", "200", "500"));
        assertTrue(ResponseAssertor.compare("gt", "500", "200"));
        assertTrue(ResponseAssertor.compare("lt", "200", "500"));
        assertTrue(ResponseAssertor.compare("contains", "abcdef", "cd"));
        assertTrue(ResponseAssertor.compare("notContains", "abcdef", "xyz"));
        // null 按空串处理，不抛异常
        assertTrue(ResponseAssertor.compare("eq", null, ""));
    }

    // ---------- 变量提取（响应头来源） ----------

    private static VarExtract e(String var, String source, String path) {
        VarExtract v = new VarExtract();
        v.setVar(var);
        v.setSource(source);
        v.setPath(path);
        return v;
    }

    /** 同一批规则里混用 body 与 header 提取 */
    @Test
    void extractFromHeaderAndBodyTogether() {
        Map<String, String> pool = new HashMap<>();
        ResponseVarExtractor.extract("{\"data\":{\"userId\":42}}", hdr("X-Token", "tk-123"),
                List.of(e("token", "header", "X-Token"), e("userId", "body", "data.userId")),
                pool, 65536);
        assertEquals("tk-123", pool.get("token"));
        assertEquals("42", pool.get("userId"));
        assertEquals(2, pool.size());
    }

    /** 未指定 source 时默认走 body，保证既有配置向后兼容 */
    @Test
    void extractDefaultsToBody() {
        Map<String, String> pool = new HashMap<>();
        VarExtract v = new VarExtract();
        v.setVar("t");
        v.setPath("data.token");
        ResponseVarExtractor.extract("{\"data\":{\"token\":\"abc\"}}", List.of(v), pool, 65536);
        assertEquals("abc", pool.get("t"));
        assertEquals("body", v.getSource());
    }

    /** 响应头缺失时不应覆盖变量池中已有的值 */
    @Test
    void extractHeaderMissingKeepsExistingValue() {
        Map<String, String> pool = new HashMap<>();
        pool.put("token", "old");
        ResponseVarExtractor.extract(null, hdr("Other", "x"),
                List.of(e("token", "header", "X-Token")), pool, 65536);
        assertEquals("old", pool.get("token"));
    }

    /** 非 JSON 响应不应中断：header 提取照常生效 */
    @Test
    void extractHeaderStillWorksWhenBodyIsNotJson() {
        Map<String, String> pool = new HashMap<>();
        ResponseVarExtractor.extract("<html>error</html>", hdr("Set-Cookie", "sid=zz"),
                List.of(e("sid", "header", "Set-Cookie"), e("x", "body", "a.b")),
                pool, 65536);
        assertEquals("sid=zz", pool.get("sid"));
        assertNull(pool.get("x"));
    }

    @Test
    void extractHeaderNameCaseInsensitive() {
        Map<String, String> pool = new HashMap<>();
        ResponseVarExtractor.extract(null, hdr("Set-Cookie", "sid=1"),
                List.of(e("sid", "header", "set-cookie")), pool, 65536);
        assertEquals("sid=1", pool.get("sid"));
    }
}
