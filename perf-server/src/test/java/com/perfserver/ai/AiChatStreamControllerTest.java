package com.perfserver.ai;

import org.junit.jupiter.api.Test;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;

/**
 * AI 流式对话控制器：排队有界，满载时快速失败。
 *
 * <p>无界队列会让突发流量下的请求静默堆积，客户端挂满
 * {@code perf.ai.stream-timeout-sec} 才超时，期间得不到任何反馈。
 */
class AiChatStreamControllerTest {

    @Test
    void rejectsRequestWhenPoolAndQueueAreFull() throws Exception {
        AiAgentService agent = mock(AiAgentService.class);
        CountDownLatch running = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        doAnswer(inv -> {
            running.countDown();
            release.await(5, TimeUnit.SECONDS);
            return Map.of("reply", "done");
        }).when(agent).chat(anyString(), anyString(), anyString(), any());

        // 线程数 1 + 队列容量 2×1 → 最多容纳 3 个请求
        AiChatStreamController controller = new AiChatStreamController(agent, 300, 1);
        try {
            for (int i = 0; i < 3; i++) {
                controller.stream(Map.of("sessionId", "s" + i, "message", "hi"));
            }
            assertTrue(running.await(5, TimeUnit.SECONDS), "首个请求应被线程池接受并执行");

            // 第 4 个：线程与队列均满，必须立即失败返回，而不是无限排队
            SseEmitter rejected = controller.stream(Map.of("sessionId", "s4", "message", "hi"));
            assertNotNull(rejected, "满载时也要返回 emitter 并立即关闭，不能让客户端挂着等到超时");

            release.countDown();
            Thread.sleep(300);
            // 关键断言：被拒绝的请求从未进入 agent，不会在队列里等待数分钟后才报错
            verify(agent, times(3)).chat(anyString(), anyString(), anyString(), any());
        } finally {
            controller.shutdown();
        }
    }
}
