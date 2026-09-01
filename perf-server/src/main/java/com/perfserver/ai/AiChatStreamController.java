package com.perfserver.ai;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.Map;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * AI 流式对话接口：{@code POST /api/ai/chat/stream}（SSE）。
 *
 * <p>事件类型见 {@link AiStreamEventType}：
 * <ul>
 *   <li>{@code token} —— 正文增量，逐 token 渲染；</li>
 *   <li>{@code thought} —— 推理过程与轮次心跳（工具执行期间无正文，防止代理层断连）；</li>
 *   <li>{@code tool} / {@code observation} —— 工具调用与返回预览；</li>
 *   <li>{@code done} —— 结束（含 mode/role/rounds）；</li>
 *   <li>{@code error} —— 异常。</li>
 * </ul>
 *
 * <p>data 负载统一为 JSON 编码后的字符串（正文含换行，直接写 text 会破坏 SSE 帧格式）。
 *
 * <p>超时取 {@code perf.ai.stream-timeout-sec}（默认 300s），需 ≥ 工具执行耗时；
 * 前端用 fetch + ReadableStream 读取（axios 不支持流式）。
 *
 * <p>并发取 {@code perf.ai.stream-threads}（默认 4）固定线程 + <b>有界队列</b>（容量 2×线程数）。
 * 线程满载时请求先入队等待，入队前推一条 {@code thought} 排队提示（否则期间无任何事件，
 * 前端表现为"发了消息没反应"）；队列也满时立即推 {@code error} 并关闭流，
 * 避免无界排队让客户端挂满 {@code perf.ai.stream-timeout-sec} 后才超时。
 */
@RestController
@RequestMapping("/api/ai")
public class AiChatStreamController {

    private static final Logger log = LoggerFactory.getLogger(AiChatStreamController.class);

    /** 排队容量系数：允许少量请求排队吸收抖动，但不做无界堆积 */
    private static final int QUEUE_FACTOR = 2;

    private final AiAgentService agent;
    private final ObjectMapper mapper = new ObjectMapper();
    private final ThreadPoolExecutor executor;
    private final long timeoutMillis;

    public AiChatStreamController(AiAgentService agent,
                                  @Value("${perf.ai.stream-timeout-sec:300}") long streamTimeoutSec,
                                  @Value("${perf.ai.stream-threads:4}") int streamThreads) {
        this.agent = agent;
        // 下限 30s：避免因误配导致正常对话被秒断
        this.timeoutMillis = Math.max(30L, streamTimeoutSec) * 1000L;
        int poolSize = Math.max(1, streamThreads);
        ThreadFactory factory = r -> {
            Thread t = new Thread(r, "ai-chat-stream");
            t.setDaemon(true);
            return t;
        };
        // 有界队列 + 默认 AbortPolicy：满载时 execute 抛 RejectedExecutionException，
        // 由调用方立即反馈，而不是让请求在队列里静默堆积到超时
        this.executor = new ThreadPoolExecutor(poolSize, poolSize, 0L, TimeUnit.MILLISECONDS,
                new LinkedBlockingQueue<>(Math.max(1, poolSize * QUEUE_FACTOR)), factory);
    }

    @PreDestroy
    public void shutdown() {
        executor.shutdownNow();
    }

    /** 流式对话：返回 SseEmitter，实际推理在线程池中异步执行 */
    @PostMapping("/chat/stream")
    public SseEmitter stream(@RequestBody Map<String, String> body) {
        String sessionId = body == null ? "default" : body.getOrDefault("sessionId", "default");
        String message = body == null ? "" : body.getOrDefault("message", "");
        String appName = body == null ? "" : body.getOrDefault("appName", "");

        SseEmitter emitter = new SseEmitter(timeoutMillis);
        AtomicBoolean closed = new AtomicBoolean(false);
        emitter.onCompletion(() -> closed.set(true));
        emitter.onError(e -> closed.set(true));
        emitter.onTimeout(() -> {
            closed.set(true);
            log.warn("ai chat stream timeout after {}ms, sessionId={}", timeoutMillis, sessionId);
            emitter.complete();
        });

        // 排队感知：固定线程池满载时新请求要在队列里等待（最长 300s），期间前端收不到任何事件，
        // 会被误认为"发了消息没反应"，故入队前先推一条 thought 提示
        if (executor.getActiveCount() >= executor.getPoolSize()) {
            send(emitter, AiStreamEvent.thought("当前并发已满，请求已排队，请稍候…"));
        }

        try {
            executor.execute(() -> {
                if (closed.get()) {
                    return;
                }
                try {
                    agent.chat(sessionId, message, appName, event -> {
                        if (closed.get()) {
                            return;
                        }
                        // 推送失败即视为客户端断开：标记关闭并上抛中断信号。
                        // 过程事件（thought/tool/observation）由 AiAgentService 吞掉该异常仅记日志；
                        // 正文 token 的异常由 LlmClient 捕获后中断读流，不再为已断开的下游消耗上游 token
                        if (!send(emitter, event)) {
                            closed.set(true);
                            throw new IllegalStateException("sse client disconnected");
                        }
                    });
                    // done 事件由 AiAgentService 在返回前推送（携带 mode/role/rounds），此处仅收尾连接
                    if (!closed.get()) {
                        emitter.complete();
                    }
                } catch (Exception e) {
                    log.warn("ai chat stream error: {}", e.getMessage());
                    closed.set(true);
                    emitter.completeWithError(e);
                }
            });
        } catch (RejectedExecutionException e) {
            // 线程与队列均满：立即告知用户，避免客户端挂着等待直到超时
            log.warn("ai chat stream rejected, pool={}, queued={}, sessionId={}",
                    executor.getPoolSize(), executor.getQueue().size(), sessionId);
            send(emitter, AiStreamEvent.error("当前对话请求过多，请稍后再试"));
            emitter.complete();
        }
        return emitter;
    }

    /**
     * 推送单个 SSE 事件。
     *
     * @return 是否推送成功；失败（客户端断开/连接已关闭）由调用方决定是否停止后续推送
     */
    private boolean send(SseEmitter emitter, AiStreamEvent event) {
        try {
            String data = mapper.writeValueAsString(event.payload());
            emitter.send(SseEmitter.event().name(event.type().eventName()).data(data));
            return true;
        } catch (Exception e) {
            log.debug("sse send failed: {}", e.getMessage());
            return false;
        }
    }
}
