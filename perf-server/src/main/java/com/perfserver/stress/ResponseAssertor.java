package com.perfserver.stress;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;
import java.util.regex.PatternSyntaxException;

/**
 * 响应结果判断（断言）：按规则校验单次响应，全部命中才算成功。
 *
 * <p>存在意义：压测里「HTTP 200」不等于业务成功。接口可能返回 200 但 body 是
 * {@code {"code":500,"message":"库存不足"}}，或网关把后端异常包装成 200。
 * 仅按状态码统计会让错误率严重失真，故提供结果判断把业务失败计入错误。
 *
 * <p>性能约束：断言在每个样本上执行，必须轻量——
 * 仅在存在 body/json 断言时才解析响应体；正则预编译并缓存于规则对象之外
 * （由调用方持有规则列表，此处按规则惰性解析，避免每次请求重复编译）。
 */
public final class ResponseAssertor {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** 参与断言解析的响应体最大字符数（超长响应截断，避免压测机被大响应拖垮） */
    static final int MAX_ASSERT_BODY_CHARS = 64 * 1024;

    private ResponseAssertor() {
    }

    /**
     * 判断是否只有状态码断言（无需读取响应体）。
     *
     * <p>用于避免无谓的 body 解析开销：纯 status/header 断言时不读 body。
     */
    public static boolean needsBody(List<StressTaskRequest.Assertion> rules) {
        if (rules == null || rules.isEmpty()) {
            return false;
        }
        for (StressTaskRequest.Assertion a : rules) {
            if (a == null) {
                continue;
            }
            String src = a.getSource() == null ? "status" : a.getSource();
            if ("body".equalsIgnoreCase(src) || "json".equalsIgnoreCase(src)) {
                return true;
            }
        }
        return false;
    }

    /**
     * 执行全部断言：全部命中返回 true，任一不命中返回 false。
     *
     * @param rules  断言规则（null/空视为无断言，直接通过）
     * @param status HTTP 状态码
     * @param headers 响应头（大小写不敏感查找）
     * @param body    响应体（{@link #needsBody} 为 false 时可为 null）
     * @return true = 本次请求判定成功
     */
    public static boolean matches(List<StressTaskRequest.Assertion> rules, int status,
                                  Map<String, List<String>> headers, String body) {
        if (rules == null || rules.isEmpty()) {
            return true;
        }
        JsonNode root = null;
        boolean rootResolved = false;
        for (StressTaskRequest.Assertion a : rules) {
            if (a == null) {
                continue;
            }
            String src = a.getSource() == null ? "status" : a.getSource();
            String actual;
            if ("status".equalsIgnoreCase(src)) {
                actual = String.valueOf(status);
            } else if ("header".equalsIgnoreCase(src)) {
                actual = headerValue(headers, a.getKey());
                // 响应头缺失：除 notContains / ne 外一律判失败
                if (actual == null) {
                    String op = a.getOp() == null ? "eq" : a.getOp();
                    if ("ne".equalsIgnoreCase(op) || "notContains".equalsIgnoreCase(op)) {
                        continue;
                    }
                    return false;
                }
            } else if ("body".equalsIgnoreCase(src)) {
                actual = body;
                if (actual == null) {
                    return false;
                }
            } else if ("json".equalsIgnoreCase(src)) {
                if (!rootResolved) {
                    root = parseJson(body);
                    rootResolved = true;
                }
                JsonNode node = root == null ? null : resolvePath(root, a.getKey());
                if (node == null || node.isNull() || node.isMissingNode()) {
                    // 路径未命中：除 ne / notContains 外判失败
                    String op = a.getOp() == null ? "eq" : a.getOp();
                    if ("ne".equalsIgnoreCase(op) || "notContains".equalsIgnoreCase(op)) {
                        continue;
                    }
                    return false;
                }
                actual = node.isValueNode() ? node.asText() : node.toString();
            } else {
                // 未知来源：忽略该断言，不因配置笔误让整轮压测全判失败
                continue;
            }
            if (!compare(a.getOp(), actual, a.getExpected())) {
                return false;
            }
        }
        return true;
    }

    /** 响应头取值（大小写不敏感；多个同名头取第一个） */
    private static String headerValue(Map<String, List<String>> headers, String name) {
        if (headers == null || name == null || name.isBlank()) {
            return null;
        }
        for (Map.Entry<String, List<String>> e : headers.entrySet()) {
            if (e.getKey() != null && e.getKey().equalsIgnoreCase(name.trim())
                    && e.getValue() != null && !e.getValue().isEmpty()) {
                return e.getValue().get(0);
            }
        }
        return null;
    }

    /**
     * 单条比较。
     *
     * @param op       比较方式；null/空按 eq 处理
     * @param actual   实际值（null 按空串处理）
     * @param expected 期望值
     */
    static boolean compare(String op, String actual, String expected) {
        String a = actual == null ? "" : actual;
        String e = expected == null ? "" : expected;
        String o = op == null || op.isBlank() ? "eq" : op.trim().toLowerCase();
        switch (o) {
            case "eq":
                return numericEq(a, e);
            case "ne":
                return !numericEq(a, e);
            case "contains":
                return a.contains(e);
            case "notcontains":
                return !a.contains(e);
            case "gt":
                return numericCompare(a, e) > 0;
            case "lt":
                return numericCompare(a, e) < 0;
            case "regex":
                return regexMatch(a, e);
            default:
                // 未知操作符：按 eq 兜底，避免配置笔误导致全失败
                return numericEq(a, e);
        }
    }

    /** 相等比较：双方均为数值时按数值比较（避免 "200" 与 "200.0" 不等的误判） */
    private static boolean numericEq(String a, String e) {
        try {
            return Double.compare(Double.parseDouble(a.trim()), Double.parseDouble(e.trim())) == 0;
        } catch (NumberFormatException ignored) {
            return a.equals(e);
        }
    }

    /** 数值比较：非数值退化为字符串字典序（保证有确定结果而非抛异常） */
    private static int numericCompare(String a, String e) {
        try {
            return Double.compare(Double.parseDouble(a.trim()), Double.parseDouble(e.trim()));
        } catch (NumberFormatException ignored) {
            return a.compareTo(e);
        }
    }

    private static boolean regexMatch(String actual, String pattern) {
        try {
            return Pattern.compile(pattern).matcher(actual).find();
        } catch (PatternSyntaxException ignored) {
            // 正则非法：视为不命中，但不抛异常中断压测
            return false;
        }
    }

    private static JsonNode parseJson(String body) {
        if (body == null || body.isBlank()) {
            return null;
        }
        try {
            String json = body.length() > MAX_ASSERT_BODY_CHARS
                    ? body.substring(0, MAX_ASSERT_BODY_CHARS) : body;
            return MAPPER.readTree(json);
        } catch (Exception e) {
            // 非 JSON 响应：无法做 json 断言
            return null;
        }
    }

    /** 按点分路径取值（与 ResponseVarExtractor 语义一致） */
    private static JsonNode resolvePath(JsonNode root, String path) {
        JsonNode current = root;
        for (String segment : path.split("\\.")) {
            if (segment.isEmpty() || current == null) {
                return null;
            }
            current = current.get(segment);
        }
        return current;
    }

    /** 便捷入口：直接从 HttpResponse 判定 */
    public static boolean matches(List<StressTaskRequest.Assertion> rules, HttpResponse<String> resp,
                                  boolean readBody) {
        return matches(rules, resp.statusCode(), resp.headers().map(),
                readBody ? resp.body() : null);
    }
}
