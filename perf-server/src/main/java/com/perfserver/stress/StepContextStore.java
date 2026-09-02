package com.perfserver.stress;

import com.fasterxml.jackson.databind.JsonNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReferenceArray;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 步骤响应上下文池：支撑「步骤上下文引用」语法 {@code {{p1.header.X-Token}}} /
 * {@code {{p2.result.data.token}}} / {@code {{login.result.user.id}}}。
 *
 * <p>每个步骤占一个槽位，只保留<b>最近一次</b>响应（scenario 是按权重随机选步的
 * 开放模型，引用的语义是「该步骤全局最近一次响应」，详见设计 2.6）。
 *
 * <p><b>并发模型</b>（多 worker 共享，读远多于写）：
 * <ul>
 *   <li><b>整体原子替换</b>：{@link AtomicReferenceArray} 一次 {@code set} 换掉整个
 *       {@link StepResponse}，读侧永远看到 header 与 body 同一版本的完整快照，
 *       不存在「头是新版、体是旧版」的撕裂；</li>
 *   <li><b>懒解析 JSON</b>：写入只存 {@code bodyRaw}，首次被 {@code {{pN.result.*}}}
 *       引用时才解析并缓存，锁是<b>槽位自身</b>（{@code synchronized(slot)} 双检），
 *       不同槽位互不阻塞，且仅首次付费；</li>
 *   <li>无全局锁，热路径（渲染取值）纯读，无竞争。</li>
 * </ul>
 *
 * <p><b>步骤引用解析的唯一实现</b>：{@link #parseStepRef(String)} 同时供「别名登记时
 * 拦截 {@code pN} 形式」与「运行期把 {@code pN} 解析成槽位」使用。两处若各写一份判断
 * （如一方用 {@code startsWith("p")}），会出现「别名侧放行、运行期命中」或反之的错位
 * ——例如 {@code p01} / {@code p1x} / {@code p999999999} 的归属出现歧义。
 *
 * <p>本类为包级可见：仅 {@link HttpStressEngine} 与 {@link TemplateRenderer} 使用。
 */
final class StepContextStore {

    private static final Logger log = LoggerFactory.getLogger(StepContextStore.class);

    /**
     * 步骤序号引用：{@code p}（大小写均可）+ 数字。
     *
     * <p>前导零（{@code p01}）与 {@code p0} 一律视为<b>非法</b>：避免同一槽位存在
     * 多种写法，也让「别名是否撞上 pN」的判定保持唯一（{@code p01} 不是 pN，
     * 因此可以作为别名；而 {@code p1} 永远指向步骤 1，不能被别名占用）。
     */
    private static final Pattern STEP_REF = Pattern.compile("^[pP](\\d+)$");

    /** 别名合法字符：字母/下划线开头，后接字母、数字、下划线、连字符 */
    private static final Pattern ALIAS = Pattern.compile("^[A-Za-z_][A-Za-z0-9_-]*$");

    private final int size;
    /** 别名 → 步骤下标（0-based）；未登记的名字一律不是上下文引用 */
    private final Map<String, Integer> aliasIndex;
    private final int maxBodyChars;
    private final AtomicReferenceArray<StepResponse> slots;

    StepContextStore(int size, Map<String, Integer> aliasIndex, int maxBodyChars) {
        this.size = size;
        this.aliasIndex = aliasIndex;
        this.maxBodyChars = maxBodyChars;
        this.slots = new AtomicReferenceArray<>(size);
    }

    /**
     * 解析 {@code pN} 形式的步骤序号（1-based）。
     *
     * <p>这是全仓库判定「某字符串是否为步骤序号引用」的唯一入口——别名登记拦截与
     * 运行期 {@link #indexOf} 共用，杜绝两处规则漂移。
     *
     * <table>
     *   <caption>边界（均为确定性规则）</caption>
     *   <tr><td>{@code p1}</td><td>1</td></tr>
     *   <tr><td>{@code P2}</td><td>2（前缀大小写不敏感）</td></tr>
     *   <tr><td>{@code p0}</td><td>-1（序号从 1 开始）</td></tr>
     *   <tr><td>{@code p01}</td><td>-1（前导零非法，避免多写法指向同一槽位）</td></tr>
     *   <tr><td>{@code p1x} / {@code p} / {@code 1p}</td><td>-1（不匹配正则）</td></tr>
     *   <tr><td>{@code p99999999999999999999}</td><td>-1（数字段溢出）</td></tr>
     * </table>
     *
     * @param ref 占位符中的 stepRef 片段
     * @return 步骤序号（1-based）；不是合法序号引用返回 -1
     */
    static int parseStepRef(String ref) {
        if (ref == null || ref.isEmpty()) {
            return -1;
        }
        Matcher m = STEP_REF.matcher(ref);
        if (!m.matches()) {
            return -1;
        }
        String digits = m.group(1);
        if (digits.length() > 1 && digits.charAt(0) == '0') {
            return -1;
        }
        long n;
        try {
            n = Long.parseLong(digits);
        } catch (NumberFormatException e) {
            return -1;
        }
        return n < 1 || n > Integer.MAX_VALUE ? -1 : (int) n;
    }

    /** 别名是否合法（不校验是否与 {@code pN} 冲突，后者由 {@link #buildAliasIndex} 拦截） */
    static boolean isValidAlias(String alias) {
        return alias != null && ALIAS.matcher(alias).matches();
    }

    /**
     * 构建「别名 → 步骤下标」表（启动期一次性）。
     *
     * <p>规则（设计 2.4）：
     * <ul>
     *   <li>{@code alias} 为空时回退用 {@code name} 作别名；</li>
     *   <li>别名不合法 / 形如 {@code pN}（{@link #parseStepRef} 命中）/ 与已登记别名重名
     *       → 登记失败并把原因写入 {@code warnings}；</li>
     *   <li><b>日志降噪</b>：回退自 {@code name} 的别名若非法（如中文名「登录」），
     *       仅 DEBUG 不写 warnings——用户并未主动声明别名，WARN 会刷屏；
     *       只有<b>显式填写</b>的 alias 出问题才告警。</li>
     * </ul>
     *
     * @param steps    场景步骤
     * @param warnings 收集告警文案（调用方负责 WARN 日志与写入结果）
     * @return 别名表；无有效别名时为空 Map
     */
    static Map<String, Integer> buildAliasIndex(List<StressTaskRequest.ScenarioStep> steps,
                                                List<String> warnings) {
        Map<String, Integer> index = new java.util.HashMap<>();
        for (int i = 0; i < steps.size(); i++) {
            StressTaskRequest.ScenarioStep step = steps.get(i);
            if (step == null) {
                continue;
            }
            String explicit = blankToNull(step.getAlias());
            String name = blankToNull(step.getName());
            // 回退用的 name 非法只 DEBUG：用户未主动声明别名，WARN 会刷屏
            String alias = explicit != null ? explicit : name;
            boolean explicitDeclared = explicit != null;
            if (!explicitDeclared && name == null) {
                continue;
            }
            if (!isValidAlias(alias)) {
                if (explicitDeclared) {
                    warnings.add("步骤 " + (i + 1) + " 的别名「" + alias
                            + "」不合法（需匹配 [A-Za-z_][A-Za-z0-9_-]*），已忽略；仍可用 p" + (i + 1) + " 引用");
                } else {
                    log.debug("scenario step {} name '{}' is not a valid alias, skipped", i + 1, alias);
                }
                continue;
            }
            if (parseStepRef(alias) > 0) {
                // 与 pN 字面冲突：pN 永远优先，别名不得占用
                if (explicitDeclared) {
                    warnings.add("步骤 " + (i + 1) + " 的别名「" + alias
                            + "」形如步骤序号引用（pN），已忽略；序号引用优先级更高");
                }
                continue;
            }
            Integer prev = index.putIfAbsent(alias, i);
            if (prev != null) {
                // 重名：首个生效
                if (explicitDeclared) {
                    warnings.add("步骤 " + (i + 1) + " 的别名「" + alias
                            + "」与步骤 " + (prev + 1) + " 重名，已忽略（首个生效）");
                }
            }
        }
        return index;
    }

    private static String blankToNull(String s) {
        return s == null || s.isBlank() ? null : s.trim();
    }

    /** 步骤数（上下界判定用） */
    int size() {
        return size;
    }

    /**
     * 把 stepRef 解析为槽位下标。
     *
     * <p>{@code pN} 走 {@link #parseStepRef}（越界返回 -1）；其余查别名表。
     * 均不命中返回 -1，此时该占位符<b>不是</b>上下文引用，应回落到扁平变量池
     * ——这是「存量扁平变量不会被新语法劫持」的关键（设计 2.2）。
     */
    int indexOf(String stepRef) {
        if (stepRef == null) {
            return -1;
        }
        int ordinal = parseStepRef(stepRef);
        if (ordinal > 0) {
            return ordinal <= size ? ordinal - 1 : -1;
        }
        Integer idx = aliasIndex.get(stepRef);
        return idx == null ? -1 : idx;
    }

    /**
     * 写入某步骤最近一次的响应（原子整体替换）。
     *
     * <p><b>调用契约</b>：仅在<b>真正收到响应</b>时调用。HTTP 错误响应（status≥400）
     * 与断言未过的响应<b>照常入池</b>（设计 2.3.5：保证下游引用「最新可达数据」，
     * 压测要压出真实错误而非静默跳过）；但网络异常（引擎 catch 分支）没有响应对象，
     * 此时<b>不得</b>调用本方法——用空快照覆盖会丢掉旧的可达数据。
     */
    void put(int stepIdx, Map<String, List<String>> headers, String body) {
        if (stepIdx < 0 || stepIdx >= size) {
            return;
        }
        String raw;
        if (body == null || body.isEmpty()) {
            raw = "";
        } else {
            raw = body.length() > maxBodyChars ? body.substring(0, maxBodyChars) : body;
        }
        slots.set(stepIdx, new StepResponse(headers, raw));
    }

    /**
     * 按 source 与 path 从指定槽位取值。
     *
     * <p>{@code result} 不传 path（或空）时返回响应体原文 {@code bodyRaw}
     * （对应 {@code {{pN.result}}}）；传 path 时按点分路径递归取值，命中
     * object/array 中间节点则返回紧凑 JSON 文本（设计 2.7）。
     *
     * @param stepIdx 槽位下标（0-based）
     * @param source  {@code header} / {@code result}
     * @param path    响应头名或 JSON 点分路径；可为 null
     * @return 命中的字符串；槽位无数据或未命中返回 null
     */
    String resolve(int stepIdx, String source, String path) {
        if (stepIdx < 0 || stepIdx >= size) {
            return null;
        }
        StepResponse slot = slots.get(stepIdx);
        if (slot == null) {
            return null;
        }
        if ("header".equalsIgnoreCase(source)) {
            return ResponseAccess.headerValue(slot.headers, path);
        }
        // result
        String p = path == null ? "" : path.trim();
        if (p.isEmpty()) {
            return slot.bodyRaw == null || slot.bodyRaw.isEmpty() ? null : slot.bodyRaw;
        }
        JsonNode node = slot.json(maxBodyChars);
        if (node == null) {
            // 非 JSON 响应：无 path 时返回原文，有 path 时无法下钻
            return null;
        }
        JsonNode target = ResponseAccess.resolvePath(node, p);
        if (target == null || target.isNull() || target.isMissingNode()) {
            return null;
        }
        return target.isValueNode() ? target.asText() : target.toString();
    }

    /** 单个步骤的响应快照（不可变，整体替换） */
    private static final class StepResponse {
        /** 响应头（保持原始大小写，查找忽略大小写）；可能为 null */
        final Map<String, List<String>> headers;
        /** 截断后的原始响应体（非 JSON 或基本类型时直接对外返回） */
        final String bodyRaw;

        /** 懒解析开关；volatile 保证 bodyJson 的安全发布 */
        private volatile boolean jsonParsed;
        /** 解析后的 JSON 根；非 JSON 响应为 null */
        private volatile JsonNode bodyJson;

        StepResponse(Map<String, List<String>> headers, String bodyRaw) {
            this.headers = headers;
            this.bodyRaw = bodyRaw;
        }

        /**
         * 取 JSON 根（懒解析 + 槽位级双检锁）。
         *
         * <p>仅首次被 {@code {{pN.result.<path>}}} 引用时付费解析；非 JSON
         * （纯文本 / HTML / XML / 基本类型）置 {@code jsonParsed=true} 且结果为 null，
         * 避免每次引用都重复解析失败的开销。
         */
        JsonNode json(int maxBodyChars) {
            if (!jsonParsed) {
                synchronized (this) {
                    if (!jsonParsed) {
                        bodyJson = ResponseAccess.parseJson(bodyRaw, maxBodyChars);
                        jsonParsed = true;
                    }
                }
            }
            return bodyJson;
        }
    }
}
