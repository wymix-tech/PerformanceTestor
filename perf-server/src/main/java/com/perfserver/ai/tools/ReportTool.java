package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.perfserver.entity.JvmMetric;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.entity.SlowSql;
import com.perfserver.entity.StressTask;
import com.perfserver.entity.TraceChain;
import com.perfserver.service.MetricQueryService;
import com.perfserver.service.SourceIssueService;
import com.perfserver.service.StressTaskService;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

/**
 * 报告生成工具：汇总 JVM 指标、慢 SQL、调用链、压测容量与性能问题，生成 Markdown 性能报告。
 */
@Component
public class ReportTool extends AbstractAiTool {

    private final MetricQueryService metricQueryService;
    private final StressTaskService stressTaskService;
    private final SourceIssueService sourceIssueService;

    public ReportTool(MetricQueryService metricQueryService, StressTaskService stressTaskService,
                      SourceIssueService sourceIssueService) {
        this.metricQueryService = metricQueryService;
        this.stressTaskService = stressTaskService;
        this.sourceIssueService = sourceIssueService;
    }

    @Override
    public String name() {
        return "generate_report";
    }

    @Override
    public String description() {
        return "生成性能检测报告（Markdown）。参数：app 应用名（可选）；minutes 统计最近多少分钟（默认 60）。"
                + "报告包含 JVM 指标概览、慢 SQL 列表、热点调用链、压测与容量评估、性能问题与方案状态，可用于存档或分享。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"app", "minutes"}, null).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        String app = str(args, "app", null);
        int minutes = integer(args, "minutes", 60);
        try {
            LocalDateTime end = LocalDateTime.now();
            StringBuilder sb = new StringBuilder();
            sb.append("# 性能检测报告\n\n");
            sb.append("- 生成时间：").append(end).append("\n");
            sb.append("- 统计范围：最近 ").append(minutes).append(" 分钟");
            if (app != null && !app.isBlank()) {
                sb.append("（应用：").append(app).append("）");
            }
            sb.append("\n\n");

            // 1. JVM 概览
            List<JvmMetric> metrics = metricQueryService.jvmMetrics(app, end.minusMinutes(minutes), end, 1);
            sb.append("## 一、JVM 指标概览\n\n");
            if (metrics.isEmpty()) {
                sb.append("> 窗口内无 JVM 指标上报（应用可能未挂载 agent）。\n\n");
            } else {
                JvmMetric m = metrics.get(metrics.size() - 1);
                sb.append("| 指标 | 数值 |\n|---|---|\n");
                sb.append("| 堆内存使用 | ").append(m.getHeapUsedMb()).append(" MB / ")
                        .append(m.getHeapMaxMb()).append(" MB |\n");
                sb.append("| 非堆内存 | ").append(m.getNonHeapUsedMb()).append(" MB |\n");
                sb.append("| Young GC 次数 | ").append(m.getYoungGcCount()).append(" |\n");
                sb.append("| Full GC 次数 | ").append(m.getFullGcCount()).append(" |\n");
                sb.append("| 线程数 | ").append(m.getThreadCount()).append(" |\n");
                sb.append("| CPU 使用率 | ").append(m.getCpuUsage()).append("% |\n\n");
            }

            // 2. 慢 SQL
            List<SlowSql> slowSqls = metricQueryService.slowSqls(app, end.minusMinutes(minutes), end, null, 20);
            sb.append("## 二、慢 SQL（").append(slowSqls.size()).append(" 条）\n\n");
            if (slowSqls.isEmpty()) {
                sb.append("> 无慢 SQL。\n\n");
            } else {
                for (SlowSql s : slowSqls) {
                    sb.append("- **").append(s.getCostMs()).append("ms** [").append(s.getSource()).append("] ")
                            .append(truncate(s.getSqlText(), 100)).append("\n");
                }
                sb.append("\n");
            }

            // 3. 热点调用链
            List<TraceChain> traces = metricQueryService.traces(app, null, end.minusMinutes(minutes), end, 10);
            sb.append("## 三、热点调用链 TOP").append(traces.size() > 10 ? 10 : traces.size()).append("\n\n");
            if (traces.isEmpty()) {
                sb.append("> 无调用链数据。\n\n");
            } else {
                for (TraceChain t : traces) {
                    sb.append("- **").append(t.getTotalMs()).append("ms** `").append(t.getRootMethod()).append("`\n");
                }
                sb.append("\n");
            }

            // 4. 压测与容量
            List<StressTask> tasks = stressTaskService.list(null, 5);
            sb.append("## 四、最近压测与容量评估\n\n");
            if (tasks.isEmpty()) {
                sb.append("> 暂无压测任务。\n\n");
            } else {
                for (StressTask t : tasks) {
                    JsonNode cap = stressTaskService.parseResultJson(t).path("capacity");
                    sb.append("- 任务#").append(t.getId()).append(" `").append(t.getTargetUrl()).append("` ")
                            .append("并发 ").append(t.getConcurrency()).append(" / 状态 ").append(t.getStatus());
                    if (!cap.isMissingNode() && !cap.isNull()) {
                        sb.append(" / 最大并发 ").append(cap.path("maxConcurrency").asInt())
                                .append(" / 最大QPS ").append(cap.path("maxQps").asInt());
                    }
                    sb.append("\n");
                }
                sb.append("\n");
            }

            // 5. 性能问题
            List<PerformanceIssue> issues = sourceIssueService.listIssues(null, null, null, 20);
            sb.append("## 五、性能问题与修复方案（").append(issues.size()).append(" 条）\n\n");
            if (issues.isEmpty()) {
                sb.append("> 暂无性能问题记录。\n\n");
            } else {
                for (PerformanceIssue i : issues) {
                    sb.append("- #").append(i.getId()).append(" [").append(i.getIssueType()).append("] ")
                            .append(i.getTitle()).append("（状态：").append(i.getStatus()).append("）\n");
                }
                sb.append("\n");
            }
            sb.append("---\n*由 PerformanceTestor AI 助手自动生成*\n");
            return ok(Map.of("report_md", sb.toString()));
        } catch (Exception e) {
            return error("报告生成失败: " + e.getMessage());
        }
    }

    private String truncate(String s, int max) {
        if (s == null) {
            return "";
        }
        return s.length() <= max ? s : s.substring(0, max) + "...";
    }
}
