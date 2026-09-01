package com.perfserver.report;

import com.fasterxml.jackson.databind.JsonNode;
import com.perfserver.entity.StressTask;
import com.perfserver.service.StressTaskService;

import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 压测报告 HTML 渲染：将任务元信息、统计指标、分档采样点、容量评估与扩容建议
 * 渲染为一份自包含（内联 CSS、无外部资源）的 HTML 文档。
 *
 * <p>同一份 HTML 同时服务两个场景：
 * <ul>
 *   <li><b>PDF</b>：交给 openhtmltopdf 渲染（{@link ReportExportService}）；</li>
 *   <li><b>Word</b>：加 MHTML/Office XML 头后以 {@code .doc} 下载，Word 可识别 HTML 表格排版，
 *       从而零额外依赖支持「导出 Word」。</li>
 * </ul>
 *
 * <p>打印样式（A4、白底黑字、表格边框、分页控制）直接内联在 {@code <style>} 中，
 * 保证 PDF 渲染与浏览器打印（前端 {@code ReportPrintView}）观感一致。
 */
public class ReportHtmlRenderer {

    /** 时间格式化（报告统一 年-月-日 时:分:秒） */
    private static final DateTimeFormatter FMT = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    private ReportHtmlRenderer() {
    }

    /**
     * 渲染完整报告 HTML。
     *
     * @param task      压测任务（元信息）
     * @param result    结果 JSON（统计 / 分档 / 容量），null 表示任务未完成
     * @param printMode true = 追加打印脚本与页脚分页（打印页用）
     */
    public static String render(StressTask task, JsonNode result, boolean printMode) {
        StringBuilder html = new StringBuilder(4096);
        // 注意：全篇须为「良构 XHTML」——openhtmltopdf 用 XML 解析器（TRaX）读取，
        // 非空元素必须闭合、void 元素（meta/br/hr）必须自闭合，否则 PDF 生成直接失败。
        html.append("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\" />");
        html.append("<title>压测报告 - ").append(esc(task == null ? "" : task.getTaskName())).append("</title>");
        html.append("<style>").append(css()).append("</style></head><body>");
        html.append("<div class=\"report\">");
        renderHeader(html, task);
        renderSummary(html, task, result);
        renderMetrics(html, result);
        renderStages(html, result);
        renderCapacity(html, result);
        renderAdvice(html, result);
        html.append("</div>");
        if (printMode) {
            // CDATA 包裹：脚本内容含 < 等字符时不破坏 XML 良构性
            html.append("<script><![CDATA[window.addEventListener('load',function(){window.focus();});]]></script>");
        }
        html.append("</body></html>");
        return html.toString();
    }

    // ==================== 各区块渲染 ====================

    private static void renderHeader(StringBuilder html, StressTask task) {
        html.append("<h1 class=\"title\">性能压测报告</h1>");
        html.append("<div class=\"subtitle\">").append(esc(task == null ? "" : task.getTaskName()))
                .append("</div>");
        html.append("<div class=\"meta\">生成时间：").append(FMT.format(java.time.LocalDateTime.now())).append("</div>");
        if (task == null) {
            return;
        }
        html.append("<table class=\"info\"><tbody>");
        row(html, "任务编号", String.valueOf(task.getId()));
        row(html, "目标地址", task.getMethod() + " " + task.getTargetUrl());
        row(html, "压测引擎", engineLabel(task.getEngineType()));
        row(html, "并发数", task.getConcurrency() + " 并发");
        row(html, "压测时长", task.getDurationSec() + " 秒");
        if (task.getQpsLimit() != null && task.getQpsLimit() > 0) {
            row(html, "QPS 上限", task.getQpsLimit() + " （0 表示不限）");
        }
        if (task.getAppName() != null && !task.getAppName().isBlank()) {
            row(html, "归属应用", task.getAppName()
                    + (task.getAppVersion() != null && !task.getAppVersion().isBlank()
                    ? "（版本 " + task.getAppVersion() + "）" : ""));
        }
        row(html, "任务状态", statusLabel(task.getStatus()));
        if (task.getCreateTime() != null) {
            row(html, "创建时间", task.getCreateTime().format(FMT));
        }
        if (task.getFinishTime() != null) {
            row(html, "结束时间", task.getFinishTime().format(FMT));
        }
        if (task.getErrorMsg() != null && !task.getErrorMsg().isBlank()) {
            row(html, "错误信息", task.getErrorMsg());
        }
        html.append("</tbody></table>");
    }

    /** 核心统计卡片：QPS / P95 / 错误率 / 总请求 */
    private static void renderSummary(StringBuilder html, StressTask task, JsonNode result) {
        if (result == null) {
            html.append("<div class=\"empty\">任务尚未产生结果（状态：")
                    .append(esc(task == null ? "未知" : task.getStatus())).append("）</div>");
            return;
        }
        html.append("<h2>核心指标</h2>");
        html.append("<div class=\"cards\">");
        card(html, "QPS", num(result, "qps", "#,##0.0"), "");
        card(html, "P95 延迟", num(result, "p95Ms", "#,##0") + " ms", "danger");
        card(html, "错误率", num(result, "errorRate", "#,##0.00") + " %",
                dbl(result, "errorRate") > 1 ? "danger" : "");
        card(html, "总请求数", num(result, "totalRequests", "#,##0"), "");
        html.append("</div>");
    }

    /** 完整指标表 */
    private static void renderMetrics(StringBuilder html, JsonNode result) {
        if (result == null) {
            return;
        }
        html.append("<h2>响应时间分布</h2>");
        html.append("<table class=\"grid\"><thead><tr>")
                .append("<th>指标</th><th>P50</th><th>P95</th><th>P99</th><th>最大</th><th>平均</th>")
                .append("</tr></thead><tbody><tr>")
                .append("<td>耗时 (ms)</td>")
                .append("<td>").append(num(result, "p50Ms", "#,##0")).append("</td>")
                .append("<td>").append(num(result, "p95Ms", "#,##0")).append("</td>")
                .append("<td>").append(num(result, "p99Ms", "#,##0")).append("</td>")
                .append("<td>").append(num(result, "maxMs", "#,##0")).append("</td>")
                .append("<td>").append(num(result, "avgMs", "#,##0")).append("</td>")
                .append("</tr></tbody></table>");

        html.append("<table class=\"grid\"><thead><tr>")
                .append("<th>总请求</th><th>错误数</th><th>错误率</th><th>预热时长</th><th>爬坡时长</th>")
                .append("</tr></thead><tbody><tr>")
                .append("<td>").append(num(result, "totalRequests", "#,##0")).append("</td>")
                .append("<td>").append(num(result, "errorCount", "#,##0")).append("</td>")
                .append("<td>").append(num(result, "errorRate", "#,##0.00")).append(" %</td>")
                .append("<td>").append(num(result, "warmupSec", "#,##0")).append(" 秒</td>")
                .append("<td>").append(num(result, "rampUpSec", "#,##0")).append(" 秒</td>")
                .append("</tr></tbody></table>");
    }

    /** 分档采样点（容量曲线数据） */
    private static void renderStages(StringBuilder html, JsonNode result) {
        JsonNode stages = result == null ? null : result.get("stagePoints");
        if (stages == null || !stages.isArray() || stages.isEmpty()) {
            return;
        }
        html.append("<h2>分档压测数据</h2>");
        html.append("<table class=\"grid\"><thead><tr>")
                .append("<th>并发</th><th>QPS</th><th>P50 (ms)</th><th>P95 (ms)</th>")
                .append("<th>P99 (ms)</th><th>错误率</th><th>请求数</th>")
                .append("</tr></thead><tbody>");
        for (JsonNode s : stages) {
            html.append("<tr>")
                    .append("<td>").append(num(s, "concurrency", "#,##0")).append("</td>")
                    .append("<td>").append(num(s, "qps", "#,##0.0")).append("</td>")
                    .append("<td>").append(num(s, "p50Ms", "#,##0")).append("</td>")
                    .append("<td>").append(num(s, "p95Ms", "#,##0")).append("</td>")
                    .append("<td>").append(num(s, "p99Ms", "#,##0")).append("</td>")
                    .append("<td>").append(num(s, "errorRate", "#,##0.00")).append(" %</td>")
                    .append("<td>").append(num(s, "totalRequests", "#,##0")).append("</td>")
                    .append("</tr>");
        }
        html.append("</tbody></table>");
    }

    /** 容量评估（含本轮新增的拟合模型与置信区间） */
    private static void renderCapacity(StringBuilder html, JsonNode result) {
        JsonNode cap = result == null ? null : result.get("capacity");
        if (cap == null || cap.isNull()) {
            return;
        }
        html.append("<h2>容量评估</h2>");
        html.append("<table class=\"info\"><tbody>");
        row(html, "目标 P95 阈值", num(cap, "targetP95Ms", "#,##0") + " ms");
        row(html, "最大并发", num(cap, "maxConcurrency", "#,##0")
                + (cap.path("extrapolated").asBoolean(false) ? "（外推估算）" : ""));
        row(html, "最大 QPS", num(cap, "maxQps", "#,##0"));
        html.append("</tbody></table>");

        JsonNode model = cap.get("model");
        if (model != null && !model.isNull()) {
            html.append("<h3>拟合模型</h3>");
            html.append("<table class=\"info\"><tbody>");
            row(html, "拟合公式", esc(model.path("formula").asText("")));
            row(html, "拟合优度 R²", num(cap, "r2", "#,##0.0000"));
            row(html, "参与拟合档位数", num(model, "points", "#,##0"));
            if (model.path("vertexConcurrency").asDouble(0) > 0) {
                row(html, "理论拐点并发", num(model, "vertexConcurrency", "#,##0.0")
                        + "（峰值 QPS " + num(model, "vertexQps", "#,##0.0") + "）");
            }
            if (cap.path("rtKneeConcurrency").asDouble(0) > 0) {
                row(html, "RT 上翘拐点并发", num(cap, "rtKneeConcurrency", "#,##0.0"));
            }
            html.append("</tbody></table>");
        }

        // 各档 95% 置信区间
        JsonNode cis = cap.get("confidenceInterval");
        if (cis != null && cis.isArray() && !cis.isEmpty()) {
            html.append("<h3>分档 95% 置信区间</h3>");
            html.append("<table class=\"grid\"><thead><tr>")
                    .append("<th>并发</th><th>QPS</th><th>QPS 95% CI</th>")
                    .append("<th>P95 (ms)</th><th>P95 95% CI</th>")
                    .append("</tr></thead><tbody>");
            for (JsonNode c : cis) {
                html.append("<tr>")
                        .append("<td>").append(num(c, "concurrency", "#,##0")).append("</td>")
                        .append("<td>").append(num(c, "qps", "#,##0.0")).append("</td>")
                        .append("<td>").append(num(c, "qpsLower", "#,##0.0")).append(" ~ ")
                        .append(num(c, "qpsUpper", "#,##0.0")).append("</td>")
                        .append("<td>").append(num(c, "p95Ms", "#,##0")).append("</td>")
                        .append("<td>").append(num(c, "p95Lower", "#,##0")).append(" ~ ")
                        .append(num(c, "p95Upper", "#,##0")).append("</td>")
                        .append("</tr>");
            }
            html.append("</tbody></table>");
        }
    }

    /** 扩容建议 */
    private static void renderAdvice(StringBuilder html, JsonNode result) {
        JsonNode cap = result == null ? null : result.get("capacity");
        JsonNode advices = cap == null ? null : cap.get("advices");
        if (advices == null || !advices.isArray() || advices.isEmpty()) {
            return;
        }
        html.append("<h2>扩容与配置建议</h2>");
        html.append("<table class=\"grid\"><thead><tr>")
                .append("<th>应用</th><th>类型</th><th>建议</th><th>理由</th>")
                .append("</tr></thead><tbody>");
        for (JsonNode a : advices) {
            html.append("<tr>")
                    .append("<td>").append(esc(a.path("appName").asText(""))).append("</td>")
                    .append("<td>").append(esc(a.path("type").asText(""))).append("</td>")
                    .append("<td>").append(esc(a.path("advice").asText(""))).append("</td>")
                    .append("<td>").append(esc(a.path("reason").asText(""))).append("</td>")
                    .append("</tr>");
        }
        html.append("</tbody></table>");
    }

    // ==================== 渲染工具 ====================

    /** 卡片（核心指标大数字） */
    private static void card(StringBuilder html, String label, String value, String style) {
        html.append("<div class=\"card ").append(style).append("\"><div class=\"card-label\">")
                .append(esc(label)).append("</div><div class=\"card-value\">")
                .append(esc(value)).append("</div></div>");
    }

    /** 信息表一行 */
    private static void row(StringBuilder html, String label, String value) {
        html.append("<tr><td class=\"label\">").append(esc(label))
                .append("</td><td>").append(esc(value)).append("</td></tr>");
    }

    /** JSON 数值字段按格式渲染（缺失渲染 "-"） */
    static String num(JsonNode node, String field, String pattern) {
        if (node == null) {
            return "-";
        }
        JsonNode v = node.get(field);
        if (v == null || v.isNull() || !v.isNumber()) {
            return "-";
        }
        return new java.text.DecimalFormat(pattern).format(v.asDouble());
    }

    private static double dbl(JsonNode node, String field) {
        JsonNode v = node == null ? null : node.get(field);
        return v == null || !v.isNumber() ? 0 : v.asDouble();
    }

    /** 引擎类型中文名 */
    private static String engineLabel(String type) {
        if (type == null) {
            return "-";
        }
        return switch (type.toLowerCase(Locale.ROOT)) {
            case "jmeter" -> "JMeter";
            case "http" -> "内置 HTTP 引擎";
            default -> type;
        };
    }

    /** 任务状态中文名 */
    private static String statusLabel(String status) {
        if (status == null) {
            return "-";
        }
        return switch (status) {
            case "pending" -> "等待中";
            case "running" -> "执行中";
            case "finished" -> "已完成";
            case "failed" -> "失败";
            default -> status;
        };
    }

    /** HTML 转义（防结果里的 URL/错误信息破坏文档结构） */
    static String esc(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder out = new StringBuilder(s.length() + 16);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '&' -> out.append("&amp;");
                case '<' -> out.append("&lt;");
                case '>' -> out.append("&gt;");
                case '"' -> out.append("&quot;");
                case '\'' -> out.append("&#39;");
                default -> out.append(c);
            }
        }
        return out.toString();
    }

    // ==================== 样式 ====================

    /**
     * 报告样式：屏幕与打印统一（A4、白底黑字）。
     * 打印规则：卡片/表格不跨页断裂，标题与内容保持同页。
     */
    static String css() {
        return """
                * { box-sizing: border-box; }
                body { font-family: "Microsoft YaHei", "PingFang SC", "Helvetica Neue", Arial, sans-serif;
                       margin: 0; padding: 24px; background: #ffffff; color: #1a1a1a; font-size: 13px; line-height: 1.6; }
                .report { max-width: 900px; margin: 0 auto; }
                .title { font-size: 24px; font-weight: 700; margin: 0 0 4px; text-align: center; }
                .subtitle { font-size: 15px; color: #444; text-align: center; margin-bottom: 4px; word-break: break-all; }
                .meta { font-size: 12px; color: #888; text-align: center; margin-bottom: 20px; }
                h2 { font-size: 16px; font-weight: 600; margin: 22px 0 10px; padding-left: 8px;
                     border-left: 3px solid #0EA5E9; page-break-after: avoid; }
                h3 { font-size: 14px; font-weight: 600; margin: 16px 0 8px; color: #333; page-break-after: avoid; }
                .empty { padding: 20px; text-align: center; color: #888; background: #f7f7f7; border-radius: 6px; }
                table { width: 100%; border-collapse: collapse; margin-bottom: 12px; }
                table.info td { padding: 6px 10px; border-bottom: 1px solid #eee; font-size: 13px; }
                table.info td.label { width: 150px; color: #666; background: #fafafa; font-weight: 500; }
                table.grid th { background: #f2f5f7; color: #333; font-weight: 600; text-align: left;
                                padding: 8px 10px; border: 1px solid #dde3e8; font-size: 12px; }
                table.grid td { padding: 7px 10px; border: 1px solid #e6eaee; font-size: 12px; }
                table.grid tbody tr:nth-child(even) { background: #fafbfc; }
                table.grid { page-break-inside: avoid; }
                .cards { display: block; margin-bottom: 8px; }
                .card { display: inline-block; width: 23.5%; margin-right: 1.5%; padding: 12px 10px;
                        background: #f7fafb; border: 1px solid #e2e8ea; border-radius: 6px;
                        text-align: center; vertical-align: top; page-break-inside: avoid; }
                .card:last-child { margin-right: 0; }
                .card.danger { background: #fff6f5; border-color: #f5c6c2; }
                .card-label { font-size: 11px; color: #777; margin-bottom: 4px; }
                .card-value { font-size: 20px; font-weight: 700; color: #0EA5E9; }
                .card.danger .card-value { color: #e0524a; }
                @media print {
                    body { padding: 0; font-size: 11px; }
                    .report { max-width: none; }
                    h2 { page-break-after: avoid; break-after: avoid; }
                    table.grid, .card { page-break-inside: avoid; break-inside: avoid; }
                    @page { size: A4; margin: 14mm; }
                }
                """;
    }
}
