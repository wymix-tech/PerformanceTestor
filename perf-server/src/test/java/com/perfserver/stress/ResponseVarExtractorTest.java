package com.perfserver.stress;

import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * ResponseVarExtractor 单元测试：点分路径提取、占位替换、异常输入兜底。
 */
class ResponseVarExtractorTest {

    private static final String JSON = """
            {"code":0,"data":{"token":"tk-9527","user":{"id":42}},"list":[1,2,3]}
            """;

    @Test
    void extractsNestedValueIntoPool() {
        Map<String, String> pool = new HashMap<>();
        ResponseVarExtractor.extract(JSON, List.of(extract("token", "data.token")), pool, 4096);
        assertEquals("tk-9527", pool.get("token"));
    }

    @Test
    void extractsDeeplyNestedValue() {
        Map<String, String> pool = new HashMap<>();
        ResponseVarExtractor.extract(JSON, List.of(extract("uid", "data.user.id")), pool, 4096);
        assertEquals("42", pool.get("uid"));
    }

    @Test
    void serializesNonScalarNodeAsJsonText() {
        Map<String, String> pool = new HashMap<>();
        ResponseVarExtractor.extract(JSON, List.of(extract("arr", "list")), pool, 4096);
        assertTrue(pool.get("arr").startsWith("["), "数组节点应序列化为 JSON 文本");
    }

    @Test
    void missingPathLeavesPoolUntouched() {
        Map<String, String> pool = new HashMap<>();
        ResponseVarExtractor.extract(JSON, List.of(extract("x", "data.nope")), pool, 4096);
        assertTrue(pool.isEmpty());
    }

    @Test
    void nonJsonBodyIsIgnored() {
        Map<String, String> pool = new HashMap<>();
        ResponseVarExtractor.extract("<html>502 Bad Gateway</html>",
                List.of(extract("token", "data.token")), pool, 4096);
        assertTrue(pool.isEmpty(), "非 JSON 响应不得抛异常，静默跳过");
    }

    @Test
    void nullOrEmptyInputsAreNoop() {
        Map<String, String> pool = new HashMap<>();
        ResponseVarExtractor.extract(null, List.of(extract("t", "data.token")), pool, 4096);
        ResponseVarExtractor.extract("", List.of(extract("t", "data.token")), pool, 4096);
        ResponseVarExtractor.extract(JSON, null, pool, 4096);
        ResponseVarExtractor.extract(JSON, List.of(), pool, 4096);
        assertTrue(pool.isEmpty());
    }

    @Test
    void truncatedBodyStillParsesPrefix() {
        Map<String, String> pool = new HashMap<>();
        // 限长 20 字符后 JSON 不完整：解析失败应静默跳过而非抛异常
        ResponseVarExtractor.extract(JSON, List.of(extract("token", "data.token")), pool, 20);
        assertTrue(pool.isEmpty());
    }

    @Test
    void substitutesKnownVariables() {
        Map<String, String> pool = new HashMap<>();
        pool.put("token", "tk-1");
        pool.put("uid", "42");
        assertEquals("Bearer tk-1", ResponseVarExtractor.substitute("Bearer {{token}}", pool));
        assertEquals("/api/u/42/o?t=tk-1",
                ResponseVarExtractor.substitute("/api/u/{{uid}}/o?t={{token}}", pool));
    }

    @Test
    void keepsPlaceholderWhenVariableMissing() {
        // 变量缺失时保留占位符：便于排查配置错误，而非静默变成空串导致诡异请求
        Map<String, String> pool = new HashMap<>();
        pool.put("a", "1");
        assertEquals("1/{{b}}", ResponseVarExtractor.substitute("{{a}}/{{b}}", pool));
    }

    @Test
    void substituteHandlesEdgeCases() {
        Map<String, String> pool = new HashMap<>();
        pool.put("v", "x");
        assertNull(ResponseVarExtractor.substitute(null, pool));
        assertEquals("", ResponseVarExtractor.substitute("", pool));
        assertEquals("plain", ResponseVarExtractor.substitute("plain", pool));
        // 未闭合占位符：整体原样返回
        assertEquals("{{v", ResponseVarExtractor.substitute("{{v", pool));
        // 空池：不做替换
        assertEquals("{{v}}", ResponseVarExtractor.substitute("{{v}}", new HashMap<>()));
    }

    @Test
    void substituteTrimsVariableName() {
        Map<String, String> pool = new HashMap<>();
        pool.put("token", "tk");
        assertEquals("tk", ResponseVarExtractor.substitute("{{ token }}", pool));
    }

    @Test
    void createPoolCopiesInitialVariables() {
        Map<String, String> initial = new HashMap<>();
        initial.put("token", "preset");
        Map<String, String> pool = ResponseVarExtractor.createPool(initial);
        assertEquals("preset", pool.get("token"));
        // 与初始 map 解耦：初始 map 后续改动不影响池
        initial.put("token", "changed");
        assertEquals("preset", pool.get("token"));
        assertTrue(ResponseVarExtractor.createPool(null).isEmpty());
    }

    @Test
    void describeMasksValues() {
        Map<String, String> pool = ResponseVarExtractor.createPool(null);
        pool.put("token", "super-secret-token");
        String desc = ResponseVarExtractor.describe(pool);
        assertTrue(desc.contains("token"));
        assertTrue(desc.contains("len(18)"), "只暴露长度，不泄漏凭据内容");
        assertEquals("{}", ResponseVarExtractor.describe(new HashMap<>()));
    }

    private static StressTaskRequest.VarExtract extract(String var, String path) {
        StressTaskRequest.VarExtract e = new StressTaskRequest.VarExtract();
        e.setVar(var);
        e.setPath(path);
        return e;
    }
}
