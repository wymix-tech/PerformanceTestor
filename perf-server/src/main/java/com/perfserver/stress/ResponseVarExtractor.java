package com.perfserver.stress;

import com.fasterxml.jackson.databind.JsonNode;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 响应变量提取与占位替换：支撑「登录态提取 → 后续步骤携带」的场景编排。
 *
 * <p>提取：用 Jackson {@link JsonNode} 按点分路径取值（如 {@code data.token}），
 * 未命中或响应非 JSON 时静默跳过——提取失败不应中断压测，只影响后续步骤的占位值。
 * 不引入 jsonpath 依赖：点分路径已覆盖登录态提取这一核心场景。
 *
 * <p>替换：把模板中的 {@code {{var}}} 替换为变量池中的值。
 * 变量池用 {@link ConcurrentHashMap}，读写路径均无锁。
 */
public final class ResponseVarExtractor {

    private ResponseVarExtractor() {
    }

    /**
     * 按提取规则从响应取值并写入变量池。
     *
     * <p>规则可来自响应体 JSON（{@code source=body}，按点分路径）或响应头
     * （{@code source=header}，按头名，大小写不敏感）。两类规则在同一批里混用，
     * 便于「从 Set-Cookie 取 session、从 body 取 userId」这类组合场景。
     *
     * @param body    响应体（JSON）
     * @param headers 响应头（大小写不敏感）
     * @param rules   提取规则（null/空直接返回）
     * @param pool    任务级变量池（并发安全）
     * @param maxBodyChars 参与解析的响应体最大字符数（超长响应截断，避免压测机被大响应拖垮）
     */
    public static void extract(String body, Map<String, List<String>> headers,
                               List<StressTaskRequest.VarExtract> rules,
                               Map<String, String> pool, int maxBodyChars) {
        if (rules == null || rules.isEmpty()) {
            return;
        }
        JsonNode root = null;
        boolean rootResolved = false;
        for (StressTaskRequest.VarExtract rule : rules) {
            if (rule == null || rule.getVar() == null || rule.getPath() == null) {
                continue;
            }
            String source = rule.getSource() == null ? "body" : rule.getSource();
            if ("header".equalsIgnoreCase(source)) {
                String v = ResponseAccess.headerValue(headers, rule.getPath());
                if (v != null) {
                    pool.put(rule.getVar(), v);
                }
                continue;
            }
            if (body == null || body.isBlank()) {
                continue;
            }
            if (!rootResolved) {
                root = ResponseAccess.parseJson(body, maxBodyChars);
                rootResolved = true;
            }
            if (root == null) {
                continue;
            }
            JsonNode node = ResponseAccess.resolvePath(root, rule.getPath());
            if (node == null || node.isNull() || node.isMissingNode()) {
                continue;
            }
            String value = node.isValueNode() ? node.asText() : node.toString();
            pool.put(rule.getVar(), value);
        }
    }

    /**
     * 仅从响应体提取（历史签名，供既有调用方与单测使用）。
     *
     * @see #extract(String, Map, List, Map, int)
     */
    public static void extract(String body, List<StressTaskRequest.VarExtract> rules,
                               Map<String, String> pool, int maxBodyChars) {
        extract(body, null, rules, pool, maxBodyChars);
    }

    /**
     * 按点分路径取值（如 {@code data.token}）。
     *
     * <p>实现委托 {@link ResponseAccess#resolvePath}，与断言侧共用同一份逻辑。
     *
     * @return 命中节点；未命中返回 null
     */
    static JsonNode resolve(JsonNode root, String path) {
        return ResponseAccess.resolvePath(root, path);
    }

    /**
     * 替换模板中的 {@code {{var}}} 占位符。
     *
     * <p>变量不存在时保留原占位符（便于排查配置错误，而不是静默变成空串导致诡异请求）。
     *
     * @param template 模板（URL / 请求头值 / body）
     * @param pool     变量池
     * @return 替换后的字符串；模板无占位符时原样返回
     * @deprecated 逻辑已迁至 {@link TemplateRenderer}，后者额外支持
     *             {@code {{pN.result.x}}} / {@code {{alias.header.x}}} 步骤上下文引用。
     *             本方法保留仅为兼容存量调用方与单测，等价于「无上下文池」的双池渲染。
     */
    @Deprecated
    public static String substitute(String template, Map<String, String> pool) {
        return TemplateRenderer.render(template, pool, null, null);
    }

    /** 创建任务级变量池（用请求中的初始变量填充） */
    public static Map<String, String> createPool(Map<String, String> initial) {
        ConcurrentHashMap<String, String> pool = new ConcurrentHashMap<>();
        if (initial != null) {
            pool.putAll(initial);
        }
        return pool;
    }

    /** 日志脱敏：仅记录变量名与值长度，不打印凭据内容 */
    static String describe(Map<String, String> pool) {
        if (pool.isEmpty()) {
            return "{}";
        }
        StringBuilder sb = new StringBuilder("{");
        boolean first = true;
        for (Map.Entry<String, String> e : pool.entrySet()) {
            if (!first) {
                sb.append(", ");
            }
            sb.append(e.getKey()).append("=len(").append(e.getValue() == null ? 0 : e.getValue().length()).append(')');
            first = false;
        }
        return sb.append('}').toString();
    }
}
