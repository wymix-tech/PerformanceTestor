package com.perftestor.datagen.util;

/**
 * HTTP 接口路径归一化单点工具。
 *
 * <p>宿主存在两条接口解析链路：正则实现 {@code EndpointScanner} 与 AST 实现
 * {@code EndpointIndexParser}。两者若各自实现归一化，一旦出现分歧（尾斜杠、
 * 多值 path、缺前导斜杠），按 {@code (httpMethod, path)} 的精确匹配会静默降级，
 * 且难以定位。故收敛为本类，两条链路共用，保证产出完全一致。</p>
 *
 * <p>路径变量（{@code {id}}、{@code {userId}}）原样保留，不做替换——替换后会与
 * Spring 的 {@code /{id}} 语义错位。</p>
 */
public final class EndpointPath {

    private EndpointPath() {
    }

    /**
     * 拼接类级前缀与方法级路径，产出完整路径。
     *
     * @param classPrefix  类级 @RequestMapping 路径（可空/空串）
     * @param methodPath   方法级 @GetMapping 等路径（可空/空串）
     * @return 归一化后的完整路径；两者皆空时返回 "/"
     */
    public static String join(String classPrefix, String methodPath) {
        String a = normalize(classPrefix);
        String b = normalize(methodPath);
        if (a.isEmpty() && b.isEmpty()) {
            return "/";
        }
        String joined = a + b;
        return joined.isEmpty() ? "/" : stripTrailingSlash(joined);
    }

    /**
     * 单段路径归一化：去空白、补前导斜杠、去尾部斜杠（保留根 "/"）。
     *
     * @param p 原始路径（可空）
     * @return 归一化结果；空输入返回空串
     */
    public static String normalize(String p) {
        if (p == null || p.isBlank()) {
            return "";
        }
        String s = p.trim();
        if (!s.startsWith("/")) {
            s = "/" + s;
        }
        return stripTrailingSlash(s);
    }

    /** 去尾部斜杠，保留根 "/" */
    public static String stripTrailingSlash(String s) {
        if (s == null || s.isEmpty()) {
            return "";
        }
        String r = s;
        while (r.endsWith("/") && r.length() > 1) {
            r = r.substring(0, r.length() - 1);
        }
        return r;
    }

    /** 从文件路径取 simpleName（去目录与 .java 后缀），与宿主 EndpointScanner 的 className 语义一致 */
    public static String simpleNameOf(String filePath) {
        if (filePath == null) {
            return "";
        }
        String name = filePath;
        int slash = Math.max(name.lastIndexOf('/'), name.lastIndexOf('\\'));
        if (slash >= 0) {
            name = name.substring(slash + 1);
        }
        if (name.endsWith(".java")) {
            name = name.substring(0, name.length() - ".java".length());
        }
        return name;
    }
}
