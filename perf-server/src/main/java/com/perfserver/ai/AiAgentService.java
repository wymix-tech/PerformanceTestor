package com.perfserver.ai;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.ai.LlmClient.ChatResult;
import com.perfserver.ai.LlmClient.ToolCall;
import com.perfserver.common.Texts;
import com.perfserver.entity.JvmMetric;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.entity.SlowSql;
import com.perfserver.entity.StressTask;
import com.perfserver.service.MetricQueryService;
import com.perfserver.service.SourceIssueService;
import com.perfserver.service.StressTaskService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * AI 编排服务：Function Calling 多轮循环。
 *
 * <p>维护按 sessionId 隔离的对话历史；每轮将消息与工具清单发送给大模型，
 * 若模型请求工具则执行并把结果以 tool 角色回填，循环至模型给出最终回复。
 * 无 API Key 或调用失败时降级为规则模板模式（基于真实库数据演示完整闭环）。
 */
@Service
public class AiAgentService {

    private static final Logger log = LoggerFactory.getLogger(AiAgentService.class);
    /** 闭环自动执行（灌数+压测+三项观测+分析+报告）最多需 8 轮左右，留余量 */
    private static final int MAX_ROUNDS = 10;
    private static final int MAX_HISTORY = 20;

    private final LlmClient llmClient;
    private final PromptBuilder promptBuilder;
    private final MetricQueryService metricQueryService;
    private final StressTaskService stressTaskService;
    private final SourceIssueService sourceIssueService;
    private final ObjectMapper mapper = new ObjectMapper();
    private final Map<String, AiTool> toolIndex = new ConcurrentHashMap<>();
    private final Map<String, List<ObjectNode>> sessions = new ConcurrentHashMap<>();

    public AiAgentService(LlmClient llmClient, PromptBuilder promptBuilder, List<AiTool> tools,
                          MetricQueryService metricQueryService, StressTaskService stressTaskService,
                          SourceIssueService sourceIssueService) {
        this.llmClient = llmClient;
        this.promptBuilder = promptBuilder;
        this.metricQueryService = metricQueryService;
        this.stressTaskService = stressTaskService;
        this.sourceIssueService = sourceIssueService;
        for (AiTool tool : tools) {
            toolIndex.put(tool.name(), tool);
        }
    }

    /**
     * 处理用户消息并返回回复（含工具调用过程记录）。
     *
     * @return {reply: 最终回复文本, toolCalls: [{name, args, result}], mode: llm|rule}
     */
    public Map<String, Object> chat(String sessionId, String message) {
        String sid = sessionId == null || sessionId.isBlank() ? "default" : sessionId;
        List<ObjectNode> messages = sessions.computeIfAbsent(sid, k -> new ArrayList<>());
        if (messages.isEmpty()) {
            ObjectNode sys = mapper.createObjectNode();
            sys.put("role", "system");
            sys.put("content", promptBuilder.buildSystemPrompt());
            messages.add(sys);
        }
        ObjectNode user = mapper.createObjectNode();
        user.put("role", "user");
        user.put("content", message == null ? "" : message);
        messages.add(user);
        trim(messages);

        List<Map<String, Object>> toolLog = new ArrayList<>();
        String reply = "";
        int rounds = 0;
        while (rounds < MAX_ROUNDS) {
            ChatResult result = llmClient.chatWithTools(new ArrayList<JsonNode>(messages), promptBuilder.buildTools());
            if (result == null) {
                // 无 API Key / 调用失败：降级规则模板（基于真实数据）
                reply = fallbackReply(message);
                ObjectNode as = mapper.createObjectNode();
                as.put("role", "assistant");
                as.put("content", reply);
                messages.add(as);
                return Map.of("reply", reply, "toolCalls", toolLog, "mode", "rule");
            }
            if (result.getToolCalls().isEmpty()) {
                reply = result.getContent() == null ? "" : result.getContent();
                ObjectNode as = mapper.createObjectNode();
                as.put("role", "assistant");
                as.put("content", reply);
                messages.add(as);
                break;
            }
            // 回填模型请求的工具调用
            ObjectNode as = mapper.createObjectNode();
            as.put("role", "assistant");
            as.put("content", result.getContent() == null ? "" : result.getContent());
            ArrayNode calls = as.putArray("tool_calls");
            for (ToolCall tc : result.getToolCalls()) {
                ObjectNode c = calls.addObject();
                c.put("id", tc.getId());
                c.put("type", "function");
                ObjectNode fn = c.putObject("function");
                fn.put("name", tc.getName());
                fn.put("arguments", tc.getArguments());
            }
            messages.add(as);
            // 逐个执行并回填 tool 结果
            for (ToolCall tc : result.getToolCalls()) {
                String output = executeTool(tc.getName(), tc.getArguments());
                ObjectNode toolMsg = mapper.createObjectNode();
                toolMsg.put("role", "tool");
                toolMsg.put("tool_call_id", tc.getId());
                toolMsg.put("content", output);
                messages.add(toolMsg);
                toolLog.add(Map.of("name", tc.getName(), "args", tc.getArguments(), "result", Texts.truncate(output, 300)));
            }
            trim(messages);
            rounds++;
        }
        return Map.of("reply", reply, "toolCalls", toolLog, "mode", "llm");
    }

    /** 清空会话历史 */
    public void clear(String sessionId) {
        sessions.remove(sessionId);
    }

    private String executeTool(String name, String args) {
        AiTool tool = toolIndex.get(name);
        if (tool == null) {
            ObjectNode err = mapper.createObjectNode();
            err.put("error", "未知工具: " + name);
            return err.toString();
        }
        try {
            return tool.execute(args);
        } catch (Exception e) {
            log.warn("tool {} execute error: {}", name, e.getMessage());
            ObjectNode err = mapper.createObjectNode();
            err.put("error", "工具执行异常: " + e.getMessage());
            return err.toString();
        }
    }

    /**
     * 无 API Key 时的规则降级回复：按关键词匹配真实库数据，演示慢 SQL 分析、容量评估、
     * 问题列表与报告生成等核心能力。
     */
    private String fallbackReply(String message) {
        String msg = message == null ? "" : message;
        try {
            if (msg.contains("报告")) {
                return executeTool("generate_report", "{}").replace("\\n", "\n");
            }
            if (msg.contains("慢") && (msg.contains("SQL") || msg.contains("sql"))) {
                List<SlowSql> sqls = sourceIssueService.recentSlowSqls(null, 5);
                StringBuilder sb = new StringBuilder();
                sb.append("最近慢 SQL（演示模式，未接入大模型 Key，基于真实库数据）：\n");
                if (sqls.isEmpty()) {
                    sb.append("- 暂无慢 SQL 记录\n");
                } else {
                    for (SlowSql s : sqls) {
                        sb.append("- ").append(s.getCostMs()).append("ms [").append(s.getSource()).append("] ")
                                .append(Texts.truncate(s.getSqlText(), 90)).append("\n");
                    }
                }
                sb.append("\n想分析根因并生成修复方案，可配置大模型 API Key 后发送\"分析最近慢 SQL 根因并给出修复方案\"。");
                return sb.toString();
            }
            if (msg.contains("容量") || msg.contains("扩容") || msg.contains("并发")) {
                List<StressTask> tasks = stressTaskService.list(null, 3);
                StringBuilder sb = new StringBuilder();
                sb.append("最近压测与容量评估（演示模式，基于真实数据）：\n");
                if (tasks.isEmpty()) {
                    sb.append("- 暂无压测任务，可发起压测后评估\n");
                } else {
                    for (StressTask t : tasks) {
                        JsonNode cap = stressTaskService.parseResultJson(t).path("capacity");
                        sb.append("- 任务#").append(t.getId()).append(" `").append(t.getTargetUrl()).append("` 并发 ")
                                .append(t.getConcurrency()).append(" 状态 ").append(t.getStatus());
                        if (!cap.isMissingNode()) {
                            sb.append(" | 最大并发 ").append(cap.path("maxConcurrency").asInt())
                                    .append(" / 最大QPS ").append(cap.path("maxQps").asInt());
                        }
                        sb.append("\n");
                    }
                }
                return sb.toString();
            }
            if (msg.contains("jvm") || msg.contains("指标") || msg.contains("内存") || msg.contains("gc")) {
                List<JvmMetric> metrics = metricQueryService.jvmMetrics(null, LocalDateTime.now().minusMinutes(60), LocalDateTime.now(), 1);
                if (metrics.isEmpty()) {
                    return "最近 60 分钟无 JVM 指标，请确认被测应用已挂载 agent 并上报。";
                }
                JvmMetric m = metrics.get(metrics.size() - 1);
                return String.format("最近 JVM 指标（演示模式，真实数据）：堆 %d/%d MB，非堆 %d MB，YoungGC %d 次，"
                                + "FullGC %d 次，线程 %d，CPU %.1f%%",
                        m.getHeapUsedMb(), m.getHeapMaxMb(), m.getNonHeapUsedMb(),
                        m.getYoungGcCount(), m.getFullGcCount(), m.getThreadCount(), m.getCpuUsage());
            }
            if (msg.contains("问题") || msg.contains("修复") || msg.contains("方案")) {
                List<PerformanceIssue> issues = sourceIssueService.listIssues(null, null, null, 10);
                StringBuilder sb = new StringBuilder();
                sb.append("性能问题列表（演示模式，真实数据）：\n");
                if (issues.isEmpty()) {
                    sb.append("- 暂无问题记录\n");
                } else {
                    for (PerformanceIssue i : issues) {
                        sb.append("- #").append(i.getId()).append(" [").append(i.getIssueType()).append("] ")
                                .append(i.getTitle()).append("（").append(i.getStatus()).append("）\n");
                    }
                }
                return sb.toString();
            }
            if (msg.contains("压测")) {
                return "我可以发起 HTTP 压测。配置大模型 API Key 后发送类似\"对 http://127.0.0.1:8080/api/apps 压测，"
                        + "并发 50，持续 30 秒\"即可执行；当前为演示模式（无 Key）。";
            }
            if (msg.contains("自动执行") || msg.contains("闭环") || msg.contains("快速开始") || msg.contains("一键")) {
                // 规则档自动执行：真实状态快照 + 执行计划（灌数/压测为异步任务，无法同步等待完成）
                List<StressTask> tasks = stressTaskService.list(null, 3);
                List<PerformanceIssue> issues = sourceIssueService.listIssues(null, null, null, 5);
                StringBuilder sb = new StringBuilder("【AI 自动执行 · 性能检测闭环】当前为演示模式（未配置大模型 Key），"
                        + "无法自主多轮编排；以下为平台真实状态与执行计划：\n\n");
                sb.append("■ 当前状态快照\n");
                sb.append("- 最近压测任务 ").append(tasks.isEmpty() ? "无" : tasks.size() + " 个（最新 #"
                        + tasks.get(0).getId() + " " + tasks.get(0).getStatus() + "）\n");
                sb.append("- 已识别性能问题 ").append(issues.size()).append(" 个\n\n");
                sb.append("■ 六步执行计划（可在页面手动完成，或配置 Key 后由我自动串联）\n");
                sb.append("1. 导入/选择项目 → 源码管理页上传或 git 导入\n");
                sb.append("2. 灌注测试数据 → 压测中心「数据准备」Tab：联表配置 + Dry-Run 试运行后正式灌数\n");
                sb.append("3. 配置压测任务 → 选项目自动带出接口清单，设并发/时长发起\n");
                sb.append("4. 获取结果 → 压测报告（QPS/RT/容量评估）\n");
                sb.append("5. 分析调用链 → 调用链页热点方法耗时分析\n");
                sb.append("6. 问题优化 → 性能问题列表（N+1/慢 SQL）源码定位 + AI 修复方案\n\n");
                sb.append("配置大模型 API Key 后重新发送\"自动执行性能检测闭环\"，我将自动依次执行灌数 → 压测 → ");
                sb.append("指标/调用链/慢 SQL 分析 → 修复方案 → 最终报告。");
                return sb.toString();
            }
        } catch (Exception e) {
            log.warn("fallback reply error: {}", e.getMessage());
        }
        return "我是 PerformanceTestor 性能检测助手。当前未配置大模型 API Key，处于演示模式（基于真实库数据）。\n"
                + "我可以：发起压测、查询 JVM 指标 / 调用链 / 慢 SQL、灌数据、分析代码问题、生成修复提示词、"
                + "容量评估与扩容建议、生成性能报告。配置 Key 后可获得大模型驱动的自然语言对话能力。";
    }

    private void trim(List<ObjectNode> messages) {
        // 保留 system 首条 + 最近 MAX_HISTORY 条
        if (messages.size() > MAX_HISTORY + 1) {
            ObjectNode sys = messages.get(0);
            List<ObjectNode> tail = new ArrayList<>(messages.subList(messages.size() - MAX_HISTORY, messages.size()));
            messages.clear();
            messages.add(sys);
            messages.addAll(tail);
        }
    }
}
