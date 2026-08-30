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
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Consumer;

/**
 * AI 编排服务：ReAct（Thought → Action → Observation）显式多轮循环。
 *
 * <p>每轮流程：
 * <ol>
 *   <li><b>Thought</b>：模型输出本轮推理（content）；</li>
 *   <li><b>Action</b>：模型请求工具（tool_calls），编排层逐个执行；</li>
 *   <li><b>Observation</b>：工具返回截断到 {@value #MAX_OBSERVATION_CHARS} 字符后回填上下文，
 *       防止大 JSON 导致上下文膨胀；</li>
 * </ol>
 * 循环至模型不再请求工具（FINISH）或达到 {@value #MAX_ROUNDS} 轮上限。
 *
 * <p>角色：按用户意图路由 {@link AiRole}，system prompt 在角色切换时原地替换（保留对话历史）。
 *
 * <p>流式：传入 {@link AiStreamEvent} 消费器时，正文增量实时推送，并在工具调用轮次间推送
 * {@code thought} 心跳事件（防止代理层因长时间无数据断开连接）。
 *
 * <p>无 API Key 或调用失败时降级为规则模板模式（基于真实库数据演示完整闭环）。
 */
@Service
public class AiAgentService {

    private static final Logger log = LoggerFactory.getLogger(AiAgentService.class);
    /** 闭环自动执行（灌数+压测+三项观测+分析+报告）最多需 8 轮左右，留余量 */
    static final int MAX_ROUNDS = 10;
    private static final int MAX_HISTORY = 20;
    /**
     * 单条 Observation 回填上下文的字符上限：工具返回（如调用链、报告）可能上千行，
     * 全量回填会在几轮内撑爆上下文窗口。
     */
    static final int MAX_OBSERVATION_CHARS = 2000;
    /** 推送给前端的 Observation 预览长度（界面展示用，不影响回填上下文） */
    private static final int OBSERVATION_PREVIEW_CHARS = 500;
    /** 规则降级模式下逐段推送的块大小（模拟流式输出） */
    private static final int FALLBACK_CHUNK_CHARS = 60;
    /** 结论入库的最短正文长度（低于该值视为寒暄/无信息量） */
    private static final int MIN_KNOWLEDGE_CHARS = 40;

    private final LlmClient llmClient;
    private final PromptBuilder promptBuilder;
    private final AiKnowledgeService knowledgeService;
    private final MetricQueryService metricQueryService;
    private final StressTaskService stressTaskService;
    private final SourceIssueService sourceIssueService;
    private final ObjectMapper mapper = new ObjectMapper();
    private final Map<String, AiTool> toolIndex = new ConcurrentHashMap<>();
    private final Map<String, List<ObjectNode>> sessions = new ConcurrentHashMap<>();
    private final Map<String, AiRole> sessionRoles = new ConcurrentHashMap<>();

    public AiAgentService(LlmClient llmClient, PromptBuilder promptBuilder, List<AiTool> tools,
                          AiKnowledgeService knowledgeService,
                          MetricQueryService metricQueryService, StressTaskService stressTaskService,
                          SourceIssueService sourceIssueService) {
        this.llmClient = llmClient;
        this.promptBuilder = promptBuilder;
        this.knowledgeService = knowledgeService;
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
     * @return {reply, toolCalls: [{name, args, result}], mode: llm|rule, role, steps: [ReAct 步骤]}
     */
    public Map<String, Object> chat(String sessionId, String message) {
        return chat(sessionId, message, null, null);
    }

    /** 流式对话：正文与过程事件经 sink 实时推送 */
    public Map<String, Object> chat(String sessionId, String message, Consumer<AiStreamEvent> sink) {
        return chat(sessionId, message, null, sink);
    }

    /**
     * 流式对话（带应用归属，供结论入库打标）。
     *
     * @param appName 关联应用名（可空，结论入库时作为 app_name）
     * @param sink    流式事件消费器（可空，空则仅同步返回）
     */
    public Map<String, Object> chat(String sessionId, String message, String appName,
                                    Consumer<AiStreamEvent> sink) {
        String sid = sessionId == null || sessionId.isBlank() ? "default" : sessionId;
        AiRole role = promptBuilder.detectRole(message);
        List<ObjectNode> messages = sessions.computeIfAbsent(sid, k -> new ArrayList<>());
        applySystemPrompt(messages, role);
        sessionRoles.put(sid, role);

        ObjectNode user = mapper.createObjectNode();
        user.put("role", "user");
        user.put("content", message == null ? "" : message);
        messages.add(user);
        trim(messages);

        List<Map<String, Object>> toolLog = new ArrayList<>();
        List<ReactStep> steps = new ArrayList<>();
        Set<String> usedTools = new LinkedHashSet<>();
        String reply = null;
        String mode = "llm";
        ReactPhase phase = ReactPhase.THINK;
        int rounds = 0;

        while (phase != ReactPhase.FINISH && rounds < MAX_ROUNDS) {
            // 轮次心跳：工具执行期间无正文增量，防止代理层断连
            emit(sink, AiStreamEvent.thought("第 " + (rounds + 1) + " 轮推理 · 角色[" + role.label() + "]"));
            ChatResult result = llmClient.chatWithTools(new ArrayList<>(messages), promptBuilder.buildTools(),
                    sink == null ? null : text -> emit(sink, AiStreamEvent.token(text)));
            if (result == null) {
                // 无 API Key / 调用失败：降级规则模板（基于真实数据）
                reply = fallbackReply(message);
                emitChunked(sink, reply);
                appendAssistant(messages, reply);
                mode = "rule";
                phase = ReactPhase.FINISH;
                break;
            }
            List<ToolCall> calls = result.getToolCalls() == null ? List.of() : result.getToolCalls();
            String thought = result.getContent() == null ? "" : result.getContent();
            if (thought.isBlank() && !calls.isEmpty()) {
                thought = "调用工具获取证据";
            }
            if (calls.isEmpty()) {
                // FINISH：模型给出最终结论
                reply = thought;
                appendAssistant(messages, reply);
                steps.add(new ReactStep(ReactPhase.FINISH, thought, null, null, null));
                phase = ReactPhase.FINISH;
                break;
            }
            if (!thought.isBlank()) {
                emit(sink, AiStreamEvent.thought(thought));
            }
            // ACT：回填 assistant（含 tool_calls）后逐个执行
            appendAssistantWithCalls(messages, thought, calls);
            for (ToolCall tc : calls) {
                usedTools.add(tc.getName());
                emit(sink, AiStreamEvent.tool(json(Map.of("name", tc.getName(), "arguments",
                        tc.getArguments() == null ? "{}" : tc.getArguments()))));
                String raw = executeTool(tc.getName(), tc.getArguments());
                // OBSERVE：截断后回填上下文
                String observation = Texts.truncate(raw, MAX_OBSERVATION_CHARS);
                appendTool(messages, tc.getId(), observation);
                steps.add(new ReactStep(ReactPhase.OBSERVE, thought, tc.getName(),
                        tc.getArguments(), Texts.truncate(observation, OBSERVATION_PREVIEW_CHARS)));
                toolLog.add(toolLogEntry(tc, observation));
                emit(sink, AiStreamEvent.observation(json(Map.of("name", tc.getName(),
                        "result", Texts.truncate(observation, OBSERVATION_PREVIEW_CHARS)))));
            }
            trim(messages);
            rounds++;
        }

        if (phase != ReactPhase.FINISH) {
            // 轮次耗尽：用已获取的证据收尾，避免返回空回复
            reply = "（已达最大推理轮次 " + MAX_ROUNDS + " 轮，以下基于已获取的证据给出阶段性结论）\n"
                    + summarize(steps);
            appendAssistant(messages, reply);
            emitChunked(sink, reply);
        }

        if (reply == null) {
            reply = "";
        }
        if ("llm".equals(mode)) {
            saveKnowledge(appName, message, reply, role, usedTools);
        }

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("reply", reply);
        out.put("toolCalls", toolLog);
        out.put("mode", mode);
        out.put("role", role.name());
        out.put("rounds", rounds);
        List<Map<String, Object>> stepMaps = new ArrayList<>(steps.size());
        for (ReactStep s : steps) {
            stepMaps.add(s.toMap());
        }
        out.put("steps", stepMaps);
        emit(sink, AiStreamEvent.done(json(Map.of("mode", mode, "role", role.name(), "rounds", rounds))));
        return out;
    }

    /** 清空会话历史（含角色状态） */
    public void clear(String sessionId) {
        sessions.remove(sessionId);
        sessionRoles.remove(sessionId);
    }

    /** 当前会话角色（无记录时返回 GENERAL） */
    public AiRole roleOf(String sessionId) {
        return sessionRoles.getOrDefault(sessionId, AiRole.GENERAL);
    }

    // ---------------- ReAct 内部实现 ----------------

    /** 写入 system prompt；角色变化时原地替换，保留已有对话历史 */
    private void applySystemPrompt(List<ObjectNode> messages, AiRole role) {
        String prompt = promptBuilder.roleFor(role);
        if (messages.isEmpty() || !"system".equals(messages.get(0).path("role").asText())) {
            ObjectNode sys = mapper.createObjectNode();
            sys.put("role", "system");
            sys.put("content", prompt);
            messages.add(0, sys);
            return;
        }
        if (!prompt.equals(messages.get(0).path("content").asText())) {
            messages.get(0).put("content", prompt);
        }
    }

    private void appendAssistant(List<ObjectNode> messages, String content) {
        ObjectNode as = mapper.createObjectNode();
        as.put("role", "assistant");
        as.put("content", content == null ? "" : content);
        messages.add(as);
        trim(messages);
    }

    private void appendAssistantWithCalls(List<ObjectNode> messages, String thought, List<ToolCall> calls) {
        ObjectNode as = mapper.createObjectNode();
        as.put("role", "assistant");
        as.put("content", thought == null ? "" : thought);
        ArrayNode arr = as.putArray("tool_calls");
        for (ToolCall tc : calls) {
            ObjectNode c = arr.addObject();
            c.put("id", tc.getId());
            c.put("type", "function");
            ObjectNode fn = c.putObject("function");
            fn.put("name", tc.getName());
            fn.put("arguments", tc.getArguments() == null ? "{}" : tc.getArguments());
        }
        messages.add(as);
    }

    private void appendTool(List<ObjectNode> messages, String toolCallId, String observation) {
        ObjectNode toolMsg = mapper.createObjectNode();
        toolMsg.put("role", "tool");
        toolMsg.put("tool_call_id", toolCallId);
        toolMsg.put("content", observation);
        messages.add(toolMsg);
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

    /** 会话结束时沉淀结论（根因分析/修复建议等长文本才有沉淀价值） */
    private void saveKnowledge(String appName, String message, String reply, AiRole role, Set<String> usedTools) {
        if (reply == null || reply.trim().length() < MIN_KNOWLEDGE_CHARS) {
            return;
        }
        String tags = role.name().toLowerCase() + (usedTools.isEmpty() ? "" : "," + String.join(",", usedTools));
        try {
            knowledgeService.save(appName, firstLine(reply), reply, tags);
        } catch (Exception e) {
            log.warn("save knowledge failed: {}", e.getMessage());
        }
    }

    /** 轮次耗尽时的收尾摘要：列出已调用工具与观察结果预览 */
    private String summarize(List<ReactStep> steps) {
        if (steps.isEmpty()) {
            return "本轮未获取到有效工具证据，请缩小问题范围后重试。";
        }
        StringBuilder sb = new StringBuilder();
        int n = 0;
        for (ReactStep s : steps) {
            if (s.action() == null) {
                continue;
            }
            n++;
            sb.append(n).append(". ").append(s.action()).append(" → ")
                    .append(Texts.truncate(s.observation(), 200)).append('\n');
        }
        return sb.length() == 0 ? "本轮未获取到有效工具证据，请缩小问题范围后重试。" : sb.toString();
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

    // ---------------- 流式工具方法 ----------------

    private static void emit(Consumer<AiStreamEvent> sink, AiStreamEvent event) {
        if (sink == null) {
            return;
        }
        try {
            sink.accept(event);
        } catch (Exception e) {
            log.debug("stream sink error: {}", e.getMessage());
        }
    }

    /** 规则降级文本分块推送（模拟逐段输出，保持前端渲染体验一致） */
    private void emitChunked(Consumer<AiStreamEvent> sink, String text) {
        if (sink == null || text == null || text.isEmpty()) {
            return;
        }
        for (int i = 0; i < text.length(); i += FALLBACK_CHUNK_CHARS) {
            emit(sink, AiStreamEvent.token(text.substring(i, Math.min(i + FALLBACK_CHUNK_CHARS, text.length()))));
        }
    }

    private String json(Object value) {
        try {
            return mapper.writeValueAsString(value);
        } catch (Exception e) {
            return "{}";
        }
    }

    private static Map<String, Object> toolLogEntry(ToolCall tc, String observation) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", tc.getName());
        m.put("args", tc.getArguments() == null ? "{}" : tc.getArguments());
        m.put("result", Texts.truncate(observation, 300));
        return m;
    }

    private static String firstLine(String text) {
        int nl = text.indexOf('\n');
        String line = nl > 0 ? text.substring(0, nl) : text;
        return line.trim();
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
}
