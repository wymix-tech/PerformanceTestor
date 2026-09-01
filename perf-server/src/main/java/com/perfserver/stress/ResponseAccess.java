package com.perfserver.stress;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.util.List;
import java.util.Map;

/**
 * 响应读取公共工具：响应头取值与响应体 JSON 解析。
 *
 * <p>{@link ResponseAssertor}（结果判断）与 {@link ResponseVarExtractor}
 * （变量提取）都需要这两项能力，收敛到一处以避免实现分叉——两处若各写一份，
 * 行为差异（如大小写敏感、截断长度）会导致「断言通过但提取失败」这类诡异问题。
 */
public final class ResponseAccess {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private ResponseAccess() {
    }

    /**
     * 响应头取值（头名大小写不敏感；多个同名头取第一个）。
     *
     * <p>HTTP 头名按 RFC 规范大小写不敏感，但容器返回的名称大小写不定，
     * 故必须忽略大小写查找，否则用户填 {@code content-type} 会取不到值。
     *
     * @return 命中返回首值；未命中返回 null
     */
    public static String headerValue(Map<String, List<String>> headers, String name) {
        if (headers == null || name == null || name.isBlank()) {
            return null;
        }
        String target = name.trim();
        for (Map.Entry<String, List<String>> e : headers.entrySet()) {
            if (e.getKey() != null && e.getKey().equalsIgnoreCase(target)
                    && e.getValue() != null && !e.getValue().isEmpty()) {
                return e.getValue().get(0);
            }
        }
        return null;
    }

    /**
     * 解析响应体为 JSON 树（超长截断）。
     *
     * <p>非 JSON 响应（HTML / 纯文本 / 错误页）返回 null 而不抛异常：
     * 解析失败不应中断压测，只影响依赖 body 的提取与断言。
     *
     * @param body         响应体
     * @param maxBodyChars 参与解析的最大字符数（截断，避免压测机被大响应拖垮）
     * @return 解析后的 JSON 树；非 JSON 或空返回 null
     */
    public static JsonNode parseJson(String body, int maxBodyChars) {
        if (body == null || body.isBlank()) {
            return null;
        }
        try {
            String json = body.length() > maxBodyChars ? body.substring(0, maxBodyChars) : body;
            return MAPPER.readTree(json);
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * 按点分路径取值（如 {@code data.token}）。
     *
     * @return 命中节点；未命中返回 null
     */
    public static JsonNode resolvePath(JsonNode root, String path) {
        JsonNode current = root;
        for (String segment : path.split("\\.")) {
            if (segment.isEmpty() || current == null) {
                return null;
            }
            current = current.get(segment);
        }
        return current;
    }
}
