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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
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
 */
@RestController
@RequestMapping("/api/ai")
public class AiChatStreamController {

    private static final Logger log = LoggerFactory.getLogger(AiChatStreamController.class);

    private final AiAgentService agent;
    private final ObjectMapper mapper = new ObjectMapper();
    private final ExecutorService executor;
    private final long timeoutMillis;

    public AiChatStreamController(AiAgentService agent,
                                  @Value("${perf.ai.stream-timeout-sec:300}") long streamTimeoutSec,
                                  @Value("${perf.ai.stream-threads:4}") int streamThreads) {
        this.agent = agent;
        // 下限 30s：避免因误配导致正常对话被秒断
        this.timeoutMillis = Math.max(30L, streamTimeoutSec) * 1000L;
        this.executor = Executors.newFixedThreadPool(Math.max(1, streamThreads), r -> {
            Thread t = new Thread(r, "ai-chat-stream");
            t.setDaemon(true);
            return t;
        });
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

        executor.execute(() -> {
            if (closed.get()) {
                return;
            }
            try {
                agent.chat(sessionId, message, appName, event -> {
                    if (closed.get()) {
                        return;
                    }
                    try {
                        String data = mapper.writeValueAsString(event.payload());
                        emitter.send(SseEmitter.event().name(event.type().eventName()).data(data));
                    } catch (Exception e) {
                        // 客户端断开/连接已关闭：停止后续推送，避免刷日志
                        closed.set(true);
                        log.debug("sse send failed: {}", e.getMessage());
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
        return emitter;
    }
}
