package com.perfserver.stress;

import org.junit.jupiter.api.Test;

import java.util.concurrent.atomic.AtomicBoolean;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 全局 QPS 虚拟时钟令牌桶测试：限速精度与 stop 响应性。
 */
class QpsLimiterTest {

    @Test
    void enforcesApproximateGlobalRate() {
        int qpsLimit = 200; // 5ms 间隔
        HttpStressEngine.QpsLimiter limiter = new HttpStressEngine.QpsLimiter(qpsLimit);
        AtomicBoolean stop = new AtomicBoolean(false);
        int permits = 20;
        long t0 = System.nanoTime();
        for (int i = 0; i < permits; i++) {
            assertTrue(limiter.acquire(stop));
        }
        long elapsedMs = (System.nanoTime() - t0) / 1_000_000;
        // 20 个许可 = 19 × 5ms 间隔；本地调度抖动放宽下界
        assertTrue(elapsedMs >= 80, "限流失效：20 个许可仅耗时 " + elapsedMs + "ms（期望 ≥ 95ms）");
        assertTrue(elapsedMs < 3000, "限流过严：" + elapsedMs + "ms");
    }

    @Test
    void stopFlagAbortsWaiting() throws Exception {
        HttpStressEngine.QpsLimiter limiter = new HttpStressEngine.QpsLimiter(1); // 1s 间隔
        AtomicBoolean stop = new AtomicBoolean(false);
        // 第一个许可立即取得
        assertTrue(limiter.acquire(stop));
        // 后台延迟置 stop：等待中的第二个许可应在 300ms 内放弃
        Thread stopper = new Thread(() -> {
            try {
                Thread.sleep(300);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
            stop.set(true);
        });
        stopper.start();
        long t0 = System.currentTimeMillis();
        assertFalse(limiter.acquire(stop));
        long elapsed = System.currentTimeMillis() - t0;
        assertTrue(elapsed < 900, "stop 响应过慢：" + elapsed + "ms");
        stopper.join(2000);
    }
}
