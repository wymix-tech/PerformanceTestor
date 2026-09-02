package com.perfserver.stress;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 双池模板渲染单测：上下文引用（{@code {{pN.result.x}}} / {@code {{alias.header.x}}}）
 * 与存量扁平变量 {@code {{var}}} 的优先级、未命中语义与防劫持边界。
 */
class TemplateRendererTest {

    private static final String JSON =
            "{\"code\":0,\"data\":{\"token\":\"tk-9527\",\"user\":{\"id\":42}},\"list\":[{\"id\":7},{\"id\":8}]}";
    private static final Map<String, List<String>> HEADERS = Map.of(
            "X-Request-Id", List.of("trace-abc"),
            "Set-Cookie", List.of("sid=1", "sid=2"));

    /** 两步上下文：步骤 1 有响应（JSON + 头），步骤 2 空槽位（首轮未到达） */
    private static StepContextStore twoSteps() {
        Map<String, Integer> alias = new HashMap<>();
        alias.put("login", 0);
        alias.put("order", 1);
        StepContextStore ctx = new StepContextStore(2, alias, 4096);
        ctx.put(0, HEADERS, JSON);
        return ctx;
    }

    @Test
    void resolvesResultByStepOrdinal() {
        StepContextStore ctx = twoSteps();
        Map<String, String> pool = new HashMap<>();
        assertEquals("tk-9527", TemplateRenderer.render("{{p1.result.data.token}}", pool, ctx, null));
        assertEquals("42", TemplateRenderer.render("{{p1.result.data.user.id}}", pool, ctx, null));
        // 中间层节点序列化为紧凑 JSON
        assertEquals("{\"id\":42}", TemplateRenderer.render("{{p1.result.data.user}}", pool, ctx, null));
    }

    @Test
    void resolvesResultArrayIndex() {
        StepContextStore ctx = twoSteps();
        Map<String, String> pool = new HashMap<>();
        assertEquals("7", TemplateRenderer.render("{{p1.result.list.0.id}}", pool, ctx, null));
        assertEquals("8", TemplateRenderer.render("{{p1.result.list.1.id}}", pool, ctx, null));
        // 越界与「非数组却按下标取」都未命中 → 保留原文
        assertEquals("{{p1.result.list.9.id}}", TemplateRenderer.render("{{p1.result.list.9.id}}", pool, ctx, null));
        assertEquals("{{p1.result.data.0}}", TemplateRenderer.render("{{p1.result.data.0}}", pool, ctx, null));
    }

    @Test
    void resolvesWholeResultBody() {
        StepContextStore ctx = twoSteps();
        Map<String, String> pool = new HashMap<>();
        // 无 path：返回响应体原文（设计 2.7）
        assertEquals(JSON, TemplateRenderer.render("{{p1.result}}", pool, ctx, null));
    }

    @Test
    void resolvesHeaderCaseInsensitively() {
        StepContextStore ctx = twoSteps();
        Map<String, String> pool = new HashMap<>();
        assertEquals("trace-abc", TemplateRenderer.render("{{p1.header.X-Request-Id}}", pool, ctx, null));
        assertEquals("trace-abc", TemplateRenderer.render("{{p1.header.x-request-id}}", pool, ctx, null));
        // 同名多值头取第一个
        assertEquals("sid=1", TemplateRenderer.render("{{p1.header.Set-Cookie}}", pool, ctx, null));
        // 头不存在 → 保留原文
        assertEquals("{{p1.header.X-Nope}}", TemplateRenderer.render("{{p1.header.X-Nope}}", pool, ctx, null));
    }

    @Test
    void resolvesByAlias() {
        StepContextStore ctx = twoSteps();
        Map<String, String> pool = new HashMap<>();
        assertEquals("tk-9527", TemplateRenderer.render("{{login.result.data.token}}", pool, ctx, null));
        assertEquals("trace-abc", TemplateRenderer.render("{{login.header.X-Request-Id}}", pool, ctx, null));
    }

    /**
     * 【评审 P3】锁定「未命中回落扁平变量」的动态语义：
     * 已注册别名 + 槽位为空 → 回落扁平变量；槽位有值 → 上下文优先。
     *
     * <p>这是设计 2.2 的既定行为（同一变量池 key 的语义随运行时状态切换）。
     * 固定该边界，防止后人把「回落」优化成「直接保留原文」或反过来劫持扁平变量。
     */
    @Test
    void registeredAliasFallsBackToFlatVarWhenSlotEmpty() {
        StepContextStore ctx = twoSteps(); // 槽位 1（别名 order）尚无数据
        Map<String, String> pool = new HashMap<>();
        pool.put("order.result.data.orderId", "flat-value");

        // 槽位为空 → 回落扁平变量
        assertEquals("flat-value", TemplateRenderer.render("{{order.result.data.orderId}}", pool, ctx, null));

        // 槽位写入后 → 上下文优先（同一 key 的语义切换）
        ctx.put(1, null, "{\"data\":{\"orderId\":\"ctx-value\"}}");
        assertEquals("ctx-value", TemplateRenderer.render("{{order.result.data.orderId}}", pool, ctx, null));
    }

    /** 未注册的 stepRef 永远是扁平变量（防劫持），且不计入未命中 */
    @Test
    void unregisteredStepRefStaysFlatVariable() {
        StepContextStore ctx = twoSteps();
        Map<String, String> pool = new HashMap<>();
        pool.put("xxx.result.yyy", "legacy");
        pool.put("other.result.data.token", "legacy-token");
        long[] miss = new long[1];

        // 形状像上下文引用，但 stepRef 既不是 pN 也未登记为别名 → 走扁平变量
        assertEquals("legacy", TemplateRenderer.render("{{xxx.result.yyy}}", pool, ctx, miss));
        assertEquals("legacy-token", TemplateRenderer.render("{{other.result.data.token}}", pool, ctx, miss));
        assertEquals(0, miss[0], "非上下文引用不得计入 renderMiss");

        // 已注册的别名：上下文优先于同名扁平变量（设计 2.2 上下文先于变量池）
        pool.put("login.result.data.token", "flat-token");
        assertEquals("tk-9527", TemplateRenderer.render("{{login.result.data.token}}", pool, ctx, miss));
        assertEquals(0, miss[0], "已注册别名命中上下文，不应计未命中");
    }

    /** 【评审 P2】pN 解析四边界：p0 / p01 / p1x / 溢出 均非法 → 保留原文 */
    @Test
    void illegalStepRefsKeepPlaceholder() {
        StepContextStore ctx = twoSteps();
        Map<String, String> pool = new HashMap<>();
        long[] miss = new long[1];
        for (String ref : new String[]{"p0", "p01", "p1x", "p", "P0", "p99999999999999999999", "p3"}) {
            String tpl = "{{" + ref + ".result.data.token}}";
            assertEquals(tpl, TemplateRenderer.render(tpl, pool, ctx, miss),
                    "非法/越界步骤引用应保留原文: " + ref);
        }
        // 只有越界的 pN（p3）计入未命中；p0/p01/p1x 不是序号引用，不计
        assertEquals(1, miss[0], "p3 是确定的 pN 引用但越界，应计入一次未命中；其余非序号引用不计");
    }

    @Test
    void outOfRangeOrdinalCountsMissButFallsBackToFlatVar() {
        StepContextStore ctx = twoSteps();
        Map<String, String> pool = new HashMap<>();
        pool.put("p3.result.data.token", "flat");
        long[] miss = new long[1];
        assertEquals("flat", TemplateRenderer.render("{{p3.result.data.token}}", pool, ctx, miss));
        assertEquals(1, miss[0]);
    }

    @Test
    void countsMissOnlyForContextRefs() {
        StepContextStore ctx = twoSteps();
        Map<String, String> pool = new HashMap<>();
        long[] miss = new long[1];
        // 三个上下文引用未命中：路径不存在、头不存在、别名槽位为空
        TemplateRenderer.render("{{p1.result.a.b}}", pool, ctx, miss);
        TemplateRenderer.render("{{p1.header.X-Nope}}", pool, ctx, miss);
        TemplateRenderer.render("{{order.result.data.orderId}}", pool, ctx, miss);
        assertEquals(3, miss[0]);
        // 扁平变量未命中不计入
        TemplateRenderer.render("{{nope}}", pool, ctx, miss);
        assertEquals(3, miss[0], "{{var}} 未命中不得计入 renderMiss");
    }

    @Test
    void mixesContextRefsWithFlatVariables() {
        StepContextStore ctx = twoSteps();
        Map<String, String> pool = new HashMap<>();
        pool.put("uid", "42");
        assertEquals("/api/u/42?t=tk-9527&trace=trace-abc",
                TemplateRenderer.render("/api/u/{{uid}}?t={{p1.result.data.token}}&trace={{p1.header.X-Request-Id}}",
                        pool, ctx, null));
    }

    @Test
    void degradesToSubstituteWithoutContext() {
        Map<String, String> pool = new HashMap<>();
        pool.put("v", "x");
        assertNull(TemplateRenderer.render(null, pool, null, null));
        assertEquals("", TemplateRenderer.render("", pool, null, null));
        assertEquals("plain", TemplateRenderer.render("plain", pool, null, null));
        // 未闭合占位符：整体原样返回
        assertEquals("{{v", TemplateRenderer.render("{{v", pool, null, null));
        // 空池 + 无上下文：不做替换
        assertEquals("{{v}}", TemplateRenderer.render("{{v}}", new HashMap<>(), null, null));
        // 变量名 trim
        assertEquals("x", TemplateRenderer.render("{{ v }}", pool, null, null));
    }

    @Test
    void handlesBoundaryPlaceholders() {
        StepContextStore ctx = twoSteps();
        Map<String, String> pool = new HashMap<>();
        // 空占位符与无点号占位符：都不是上下文引用
        assertEquals("{{}}", TemplateRenderer.render("{{}}", pool, ctx, null));
        assertEquals("{{ }}", TemplateRenderer.render("{{ }}", pool, ctx, null));
        // 首尾相连的两个占位符
        assertEquals("tk-9527-42", TemplateRenderer.render(
                "{{p1.result.data.token}}-{{p1.result.data.user.id}}", pool, ctx, null));
    }

    /** 非 JSON 响应：{{pN.result}} 返回原文，带 path 时未命中保留原文 */
    @Test
    void nonJsonBodyReturnsRawForWholeResult() {
        StepContextStore ctx = new StepContextStore(1, new HashMap<>(), 4096);
        ctx.put(0, null, "not-json-at-all");
        Map<String, String> pool = new HashMap<>();
        assertEquals("not-json-at-all", TemplateRenderer.render("{{p1.result}}", pool, ctx, null));
        assertEquals("{{p1.result.data.token}}",
                TemplateRenderer.render("{{p1.result.data.token}}", pool, ctx, null));
    }

    /** 启动期预编译扫描：只对「能解析到槽位」与「pN 越界」的引用回调 */
    @Test
    void scanContextRefsMarksOnlyResolvableSlots() {
        Map<String, Integer> alias = new HashMap<>();
        alias.put("login", 0);
        StepContextStore ctx = new StepContextStore(2, alias, 4096);
        List<String> hits = new ArrayList<>();
        List<String> outOfRange = new ArrayList<>();
        TemplateRenderer.RefSink sink = new TemplateRenderer.RefSink() {
            @Override
            public void accept(int slotIdx, boolean header) {
                hits.add(slotIdx + ":" + (header ? "header" : "result"));
            }

            @Override
            public void outOfRange(String stepRef) {
                outOfRange.add(stepRef);
            }
        };
        TemplateRenderer.scanContextRefs(
                "{{p1.result.data.token}} {{login.header.X-Request-Id}} {{p2.result.x}} "
                        + "{{p5.result.x}} {{xxx.result.yyy}} {{flatVar}}", ctx, sink);

        assertEquals(List.of("0:result", "0:header", "1:result"), hits);
        assertEquals(List.of("p5"), outOfRange);
    }
}
