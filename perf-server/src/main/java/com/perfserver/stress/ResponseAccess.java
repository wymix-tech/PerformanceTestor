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
     * <p><b>数组下标</b>：当前节点为 {@link ArrayNode} 且段为纯数字时按下标取
     * （如 {@code list.0.id}）——旧实现对数组调 {@code get("0")} 恒返回 null，
     * 该能力为纯新增。判断严格限定在 {@code isArray()} 分支内，故
     * {@code {"0":"a"}} 这类「key 恰为数字串」的 ObjectNode 仍按 key 取值，
     * 与旧行为一致（断言侧共用本方法，不得改变既有结果）。
     *
     * <p>越界、负号、非数字段、对标量节点继续下钻 —— 一律返回 null。
     *
     * @return 命中节点；未命中返回 null
     */
    public static JsonNode resolvePath(JsonNode root, String path) {
        JsonNode current = root;
        for (String segment : path.split("\\.")) {
            if (segment.isEmpty() || current == null) {
                return null;
            }
            if (current.isArray()) {
                Integer idx = parseIndex(segment);
                if (idx == null) {
                    return null;
                }
                current = current.get(idx);
            } else {
                current = current.get(segment);
            }
        }
        return current;
    }

    /**
     * 解析数组下标：仅接受无符号十进制数字（允许前导零，如 {@code 01}）。
     *
     * @return 下标；非纯数字或超出 int 范围返回 null
     */
    private static Integer parseIndex(String segment) {
        for (int i = 0; i < segment.length(); i++) {
            char c = segment.charAt(i);
            if (c < '0' || c > '9') {
                return null;
            }
        }
        try {
            long v = Long.parseLong(segment);
            return v > Integer.MAX_VALUE ? null : (int) v;
        } catch (NumberFormatException e) {
            return null;
        }
    }
}
