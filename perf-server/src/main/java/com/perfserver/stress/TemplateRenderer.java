package com.perfserver.stress;

import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 双池模板渲染器：把双大括号占位符替换为「步骤上下文引用」或「扁平变量」。
 *
 * <p>本类吸收了 {@link ResponseVarExtractor#substitute} 的手写扫描逻辑，并在其之上
 * 前置了上下文引用分支，形成双池解析（设计 2.2）：
 * <pre>
 *   1. 上下文引用：^([A-Za-z_][\w-]*)\.(header|result)(\..+)?$
 *      stepRef → 槽位（pN 直接算；否则查别名表）
 *      · pN 形式但越界        → 计入未命中，回落扁平变量
 *      · 已登记别名 + 取值失败 → 计入未命中，回落扁平变量
 *   2. 扁平变量：pool.get(name)
 *   3. 都未命中 → 保留占位符原文（便于排查配置错误，不静默变空串）
 * </pre>
 *
 * <p><b>防劫持</b>：判据是「stepRef 能否解析到合法槽位」，而不是「字符串长得像不像
 * 上下文引用」。因此存量配置里叫 {@code xxx.result.yyy} 的扁平变量，只要 {@code xxx}
 * 不是 {@code pN} 也未登记为别名，就永远走第 2 步，不会被新语法劫持。
 *
 * <p><b>性能</b>：
 * <ul>
 *   <li>沿用手写扫描（无正则回溯风险），模板无占位符时一次 {@code indexOf} 即返回；</li>
 *   <li>上下文形状判定用静态 {@link Pattern}，但先以 {@code indexOf('.') < 0}
 *       快速排除——绝大多数存量 {@code var} 占位不含点号，无需进 matcher；</li>
 *   <li>上下文引用形状在 {@link HttpStressEngine.Scenario#of} 中另有预编译，
 *       运行期仅对被引用的步骤物化响应体。</li>
 * </ul>
 *
 * <p><b>未命中计数</b>：只统计「确定的上下文引用但取不到值」，不统计扁平变量
 * 未命中——否则存量配置会被灌满无意义计数。
 */
public final class TemplateRenderer {

    /** 上下文引用形状：stepRef . (header|result) [ . path ] */
    private static final Pattern CTX_REF = Pattern.compile("^([A-Za-z_][\\w-]*)\\.(header|result)(?:\\.(.+))?$");

    private TemplateRenderer() {
    }

    /**
     * 渲染模板。
     *
     * @param template    模板（URL / 请求头值 / 请求体）
     * @param pool        扁平变量池（可为 null/空）
     * @param ctx         步骤上下文池；null 时退化为纯扁平变量渲染
     * @param missCounter 未命中计数累加器（{@code long[1]}，由调用方复用以免热路径分配）；
     *                    可为 null 表示不计数。
     *                    <b>调用方必须在每次迭代渲染后取走并清零</b>
     *                    （{@code long miss = c[0]; c[0] = 0;}），否则 warmup/stop 期
     *                    产生的未命中会残留并被正式期第一次统计一并提交，导致计数虚高。
     * @return 替换后的字符串；无占位符时原样返回
     */
    public static String render(String template, Map<String, String> pool,
                                StepContextStore ctx, long[] missCounter) {
        if (template == null || template.isEmpty() || template.indexOf("{{") < 0) {
            return template;
        }
        if (ctx == null && (pool == null || pool.isEmpty())) {
            return template;
        }
        StringBuilder out = new StringBuilder(template.length() + 32);
        int i = 0;
        while (i < template.length()) {
            int start = template.indexOf("{{", i);
            if (start < 0) {
                out.append(template, i, template.length());
                break;
            }
            int end = template.indexOf("}}", start + 2);
            if (end < 0) {
                out.append(template, i, template.length());
                break;
            }
            out.append(template, i, start);
            String name = template.substring(start + 2, end).trim();
            String value = resolvePlaceholder(name, pool, ctx, missCounter);
            // 未命中保留原文（含 {{ }}），不静默变空串
            out.append(value == null ? template.substring(start, end + 2) : value);
            i = end + 2;
        }
        return out.toString();
    }

    /**
     * 解析单个占位符。
     *
     * @return 命中值；未命中返回 null（由调用方保留原文）
     */
    /**
     * 解析占位符名的上下文引用形状。
     *
     * <p>包级可见：运行期渲染（本类）与启动期预编译（{@code HttpStressEngine.Scenario}）
     * 共用同一份形状判定，避免两处正则漂移导致「预编译认为要捕获、运行期却不解析」。
     *
     * @return 命中返回 {@code [stepRef, source, path]}（path 可为 null）；否则返回 null
     */
    static String[] parseContextRef(String name) {
        if (name == null || name.isEmpty() || name.indexOf('.') <= 0) {
            return null;
        }
        Matcher m = CTX_REF.matcher(name);
        return m.matches() ? new String[]{m.group(1), m.group(2), m.group(3)} : null;
    }

    /** 上下文引用扫描回调（启动期预编译用） */
    interface RefSink {
        /** 命中已登记槽位：slotIdx 需捕获，header 表示取响应头（否则取响应体） */
        void accept(int slotIdx, boolean header);

        /** {@code pN} 形式但序号越界（N<1 或 N>步骤数）：确定的配置错误 */
        void outOfRange(String stepRef);
    }

    /**
     * 启动期扫描模板中的上下文引用（一次遍历，无分配压力要求的冷路径）。
     *
     * <p>只对「能解析到槽位」或「pN 越界」的引用回调——形状像但 stepRef 未登记的
     * （如存量扁平变量 {@code xxx.result.yyy}）一律不回调，无需为其捕获任何响应。
     */
    static void scanContextRefs(String template, StepContextStore ctx, RefSink sink) {
        if (template == null || template.isEmpty() || ctx == null
                || template.indexOf("{{") < 0) {
            return;
        }
        int i = 0;
        while (i < template.length()) {
            int start = template.indexOf("{{", i);
            if (start < 0) {
                return;
            }
            int end = template.indexOf("}}", start + 2);
            if (end < 0) {
                return;
            }
            String[] ref = parseContextRef(template.substring(start + 2, end).trim());
            if (ref != null) {
                int ordinal = StepContextStore.parseStepRef(ref[0]);
                if (ordinal > 0) {
                    if (ordinal > ctx.size()) {
                        sink.outOfRange(ref[0]);
                    } else {
                        sink.accept(ordinal - 1, "header".equalsIgnoreCase(ref[1]));
                    }
                } else {
                    int idx = ctx.indexOf(ref[0]);
                    if (idx >= 0) {
                        sink.accept(idx, "header".equalsIgnoreCase(ref[1]));
                    }
                }
            }
            i = end + 2;
        }
    }

    /**
     * 解析单个占位符。
     *
     * @return 命中值；未命中返回 null（由调用方保留原文）
     */
    private static String resolvePlaceholder(String name, Map<String, String> pool,
                                             StepContextStore ctx, long[] missCounter) {
        if (ctx != null) {
            String[] ref = parseContextRef(name);
            if (ref != null) {
                String stepRef = ref[0];
                String source = ref[1];
                String path = ref[2];
                int ordinal = StepContextStore.parseStepRef(stepRef);
                if (ordinal > 0) {
                    // pN 形式：确定是上下文引用。越界或取值失败都计入未命中
                    if (ordinal > ctx.size()) {
                        countMiss(missCounter);
                    } else {
                        String v = ctx.resolve(ordinal - 1, source, path);
                        if (v != null) {
                            return v;
                        }
                        countMiss(missCounter);
                    }
                } else {
                    int idx = ctx.indexOf(stepRef);
                    if (idx >= 0) {
                        // 已登记别名：确定是上下文引用
                        String v = ctx.resolve(idx, source, path);
                        if (v != null) {
                            return v;
                        }
                        countMiss(missCounter);
                    }
                    // 未登记 → 不是上下文引用，视为扁平变量名（防劫持），不计未命中
                }
            }
        }
        return pool == null ? null : pool.get(name);
    }

    private static void countMiss(long[] missCounter) {
        if (missCounter != null && missCounter.length > 0) {
            missCounter[0]++;
        }
    }
}
