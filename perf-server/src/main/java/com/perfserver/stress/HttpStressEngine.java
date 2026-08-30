package com.perfserver.stress;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/**
 * HTTP 压测引擎：有界线程池并发发压 + 周期采样（实时图表）+ 分档并发（容量曲线）。
 *
 * <p>统计无锁 O(1) 内存（分片固定桶直方图，见 {@link StressStatistician}）；
 * 采样线程与发压线程分离，不阻塞发压主循环。
 *
 * <p>执行模型（单档时序）：
 * <pre>
 * |-- warmupSec（发压但不计统计，消除冷连接/JIT 污染）--|-- durationSec 正式统计 --|
 *    \-- rampUpSec 并发爬坡（worker 线性依次激活）--/
 * </pre>
 * QPS 限流为<b>全局虚拟时钟令牌桶</b>（CAS 预约槽位），替代旧的每 worker sleep
 * 整形（低并发高 QPS 下误差大）。
 */
@Component
public class HttpStressEngine implements StressEngine {

    private static final Logger log = LoggerFactory.getLogger(HttpStressEngine.class);

    /** 压测流量标记头：被测应用可据此路由影子表/隔离压测数据（不污染生产） */
    public static final String TRAFFIC_MARK_HEADER = "X-Perf-Test";

    @Override
    public String type() {
        return "http";
    }

    @Override
    public StressTaskResult run(StressTaskRequest request, StressSampleListener listener) {
        List<StressTaskRequest.Stage> stages = request.getStages();
        StressTaskResult result = new StressTaskResult();
        result.setEngineType("http");
        result.setTargetUrl(request.getTargetUrl());
        result.setDurationSec(request.getDurationSec());
        result.setWarmupSec(request.getWarmupSec());
        result.setRampUpSec(request.getRampUpSec());

        List<StressTaskResult.Sample> allSamples = new ArrayList<>();

        if (stages == null || stages.isEmpty()) {
            // 单档模式
            StressTaskRequest.Stage single = new StressTaskRequest.Stage();
            single.setConcurrency(request.getConcurrency());
            single.setDurationSec(request.getDurationSec());
            StageOutcome outcome = runStage(request, single, listener, true);
            result.setConcurrency(single.getConcurrency());
            result.setTotalRequests(outcome.total);
            result.setErrorCount(outcome.errors);
            result.setErrorRate(outcome.errorRate);
            result.setQps(outcome.qps);
            result.setP50Ms(outcome.p50);
            result.setP95Ms(outcome.p95);
            result.setP99Ms(outcome.p99);
            result.setMaxMs(outcome.max);
            result.setAvgMs(outcome.avg);
            result.setSamples(outcome.samples);
            result.setStagePoints(java.util.List.of(outcome.toStagePoint()));
        } else {
            // 分档模式（容量评估）：仅第一档执行 warmup（后续档 JIT/连接池已热）
            long total = 0;
            long errors = 0;
            List<StressTaskResult.StagePoint> points = new ArrayList<>();
            for (int i = 0; i < stages.size(); i++) {
                StressTaskRequest.Stage stage = stages.get(i);
                log.info("stress stage: concurrency={}, duration={}s", stage.getConcurrency(), stage.getDurationSec());
                StageOutcome o = runStage(request, stage, listener, i == 0);
                points.add(o.toStagePoint());
                total += o.total;
                errors += o.errors;
                allSamples.addAll(o.samples);
            }
            result.setConcurrency(request.getConcurrency());
            result.setTotalRequests(total);
            result.setErrorCount(errors);
            result.setErrorRate(total == 0 ? 0 : errors * 100.0 / total);
            result.setSamples(allSamples);
            result.setStagePoints(points);
            if (!points.isEmpty()) {
                StressTaskResult.StagePoint last = points.get(points.size() - 1);
                result.setQps(last.getQps());
                result.setP50Ms(last.getP50Ms());
                result.setP95Ms(last.getP95Ms());
                result.setP99Ms(last.getP99Ms());
            }
        }
        return result;
    }

    /**
     * 执行单档压测。
     *
     * @param withWarmup 是否执行预热期（单档/分档首档 = true；分档后续档已热跳过）
     */
    private StageOutcome runStage(StressTaskRequest request,
                                  StressTaskRequest.Stage stage,
                                  StressSampleListener listener,
                                  boolean withWarmup) {
        StressStatistician stats = new StressStatistician();
        AtomicBoolean stop = new AtomicBoolean(false);
        /** 正式统计开关：warmup 结束（或无 warmup）后置位，worker 仅在正式期 record */
        AtomicBoolean warmupDone = new AtomicBoolean(!withWarmup || request.getWarmupSec() <= 0);
        int concurrency = stage.getConcurrency();
        int durationSec = stage.getDurationSec();
        long stageStart = System.currentTimeMillis();

        // 全局 QPS 令牌桶（虚拟时钟 CAS，替代每 worker sleep 整形）
        QpsLimiter limiter = request.getQpsLimit() > 0 ? new QpsLimiter(request.getQpsLimit()) : null;

        ExecutorService pool = Executors.newFixedThreadPool(concurrency);
        HttpClient client = buildClient();
        long rampUpMs = request.getRampUpSec() * 1000L;
        for (int i = 0; i < concurrency; i++) {
            // 爬坡：worker i 在 [stageStart, stageStart+rampUpMs] 内线性依次激活
            long activateAt = rampUpMs > 0 ? stageStart + rampUpMs * i / concurrency : stageStart;
            pool.submit(new Worker(client, request, stop, stats, warmupDone, limiter, activateAt));
        }

        // 正式统计起点（warmup 结束时刻；无 warmup 即 stageStart）
        long warmupMs = withWarmup ? request.getWarmupSec() * 1000L : 0L;
        long formalStart = stageStart + warmupMs;

        // 周期采样：正式期才开始（warmup 期不发样本，窗口基线自然从正式期算起）
        int intervalMs = Math.max(1, request.getSampleIntervalSec()) * 1000;
        List<StressTaskResult.Sample> samples = new ArrayList<>();
        ScheduledExecutorService sampler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "stress-sampler");
            t.setDaemon(true);
            return t;
        });
        sampler.scheduleAtFixedRate(() -> {
            StressStatistician.WindowStats w = stats.collectWindow();
            long now = System.currentTimeMillis();
            double qps = w.getCount() * 1000.0 / intervalMs;
            StressTaskResult.Sample s = new StressTaskResult.Sample();
            s.setSampleTime(now);
            s.setConcurrency(concurrency);
            s.setQps(round2(qps));
            s.setP50Ms(StressStatistician.percentile(w.getHistogram(), 50));
            s.setP95Ms(StressStatistician.percentile(w.getHistogram(), 95));
            s.setP99Ms(StressStatistician.percentile(w.getHistogram(), 99));
            s.setErrorCount(w.getErrorCount());
            samples.add(s);
            if (listener != null) {
                try {
                    listener.onSample(s);
                } catch (Exception e) {
                    log.warn("stress sample listener failed", e);
                }
            }
        }, warmupMs + intervalMs, intervalMs, TimeUnit.MILLISECONDS);

        try {
            // 阶段一：等待 warmup 到点（分片睡眠，可被中断打断）
            sleepUntil(formalStart);
            warmupDone.set(true);
            // 阶段二：等待正式期到点
            sleepUntil(formalStart + durationSec * 1000L);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } finally {
            stop.set(true);
            // 先停采样并等待收尾（消除读到不完整 samples 的竞态），再终止发压线程池
            sampler.shutdown();
            try {
                if (!sampler.awaitTermination(2, TimeUnit.SECONDS)) {
                    sampler.shutdownNow();
                }
            } catch (InterruptedException e) {
                sampler.shutdownNow();
                Thread.currentThread().interrupt();
            }
            pool.shutdownNow();
            try {
                if (!pool.awaitTermination(10, TimeUnit.SECONDS)) {
                    log.warn("stress worker pool did not terminate in 10s");
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }

        // 阶段累计统计（整体分位数基于阶段累计直方图；QPS 按正式期时长计算）
        StressStatistician.StageSnapshot snap = stats.stageSnapshot();
        double formalElapsedSec = Math.max(1.0, (System.currentTimeMillis() - formalStart) / 1000.0);
        StageOutcome outcome = new StageOutcome();
        outcome.total = snap.getTotal();
        outcome.errors = snap.getErrorCount();
        outcome.qps = round2(outcome.total / formalElapsedSec);
        outcome.p50 = StressStatistician.percentile(snap.getHistogram(), 50);
        outcome.p95 = StressStatistician.percentile(snap.getHistogram(), 95);
        outcome.p99 = StressStatistician.percentile(snap.getHistogram(), 99);
        outcome.max = StressStatistician.max(snap.getHistogram());
        outcome.avg = round2(snap.getAvgMs());
        outcome.errorRate = outcome.total == 0 ? 0 : round2(outcome.errors * 100.0 / outcome.total);
        outcome.samples = samples;
        outcome.concurrency = concurrency;
        return outcome;
    }

    private HttpClient buildClient() {
        return HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .followRedirects(HttpClient.Redirect.NORMAL)
                .build();
    }

    /** 睡眠至指定时刻：精确到 deadline（最后一次 sleep 恰为剩余时长），长等待分片可被中断 */
    private static void sleepUntil(long deadlineMillis) throws InterruptedException {
        while (true) {
            long remaining = deadlineMillis - System.currentTimeMillis();
            if (remaining <= 0) {
                return;
            }
            Thread.sleep(Math.min(remaining, 500));
        }
    }

    /**
     * 全局 QPS 令牌桶：虚拟时钟 CAS 预约槽位。
     *
     * <p>每个发送许可预约 {@code max(now, prev) + interval}，申请者睡眠至自己的槽位时刻——
     * 全局精确整形（与 worker 数无关），替代旧的每 worker {@code sleep(1000*c/qps)}
     * （低并发高 QPS 时误差大）。睡眠分片并检查 stop，保证可及时终止。
     */
    static final class QpsLimiter {
        private final long intervalNanos;
        private final AtomicLong nextFreeNanos = new AtomicLong(0);

        QpsLimiter(int qpsLimit) {
            this.intervalNanos = 1_000_000_000L / qpsLimit;
        }

        /**
         * 获取发送许可，必要时等待至槽位时刻。
         *
         * @return true = 已取得许可；false = 等待期间 stop 置位，放弃发压
         */
        boolean acquire(AtomicBoolean stop) {
            long slotNanos;
            while (true) {
                long now = System.nanoTime();
                long prev = nextFreeNanos.get();
                slotNanos = Math.max(now, prev);
                if (nextFreeNanos.compareAndSet(prev, slotNanos + intervalNanos)) {
                    break;
                }
            }
            while (!stop.get()) {
                long remainMs = (slotNanos - System.nanoTime()) / 1_000_000;
                if (remainMs <= 0) {
                    return true;
                }
                try {
                    Thread.sleep(Math.min(remainMs, 100));
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return false;
                }
            }
            return false;
        }
    }

    /** 压测工作线程：持续发压直到 stop 标志置位 */
    private static class Worker implements Runnable {
        private final HttpClient client;
        private final StressTaskRequest request;
        private final AtomicBoolean stop;
        private final StressStatistician stats;
        /** 正式统计开关（warmup 期不 record） */
        private final AtomicBoolean warmupDone;
        /** 全局 QPS 限流（null 不限） */
        private final QpsLimiter limiter;
        /** 爬坡激活时刻（ms epoch） */
        private final long activateAt;

        Worker(HttpClient client, StressTaskRequest request, AtomicBoolean stop,
               StressStatistician stats, AtomicBoolean warmupDone, QpsLimiter limiter, long activateAt) {
            this.client = client;
            this.request = request;
            this.stop = stop;
            this.stats = stats;
            this.warmupDone = warmupDone;
            this.limiter = limiter;
            this.activateAt = activateAt;
        }

        @Override
        public void run() {
            try {
                // 爬坡等待：激活时刻前分片睡眠（stop 置位提前退出）
                while (!stop.get()) {
                    long remain = activateAt - System.currentTimeMillis();
                    if (remain <= 0) {
                        break;
                    }
                    Thread.sleep(Math.min(remain, 50));
                }
                HttpRequest requestObj = buildRequest();
                while (!stop.get()) {
                    if (limiter != null && !limiter.acquire(stop)) {
                        return;
                    }
                    long t0 = System.nanoTime();
                    boolean error = false;
                    try {
                        HttpResponse<String> resp = client.send(requestObj, HttpResponse.BodyHandlers.ofString());
                        if (resp.statusCode() >= 400) {
                            error = true;
                        }
                    } catch (Exception e) {
                        error = true;
                    }
                    long costMs = (System.nanoTime() - t0) / 1_000_000;
                    // stop 置位后（含 shutdownNow 中断的在途请求）不记录：
                    // 收尾期结果不具代表性，且中断异常会被误计为业务错误
                    if (warmupDone.get() && !stop.get()) {
                        stats.record(costMs, error);
                    }
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }

        private HttpRequest buildRequest() {
            HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(request.getTargetUrl()));
            // 压测流量标记（默认注入；被测应用可据此路由影子表/隔离数据，用户显式设置不覆盖）
            if (request.getHeaders() == null || !request.getHeaders().containsKey(TRAFFIC_MARK_HEADER)) {
                builder.header(TRAFFIC_MARK_HEADER, "1");
            }
            if (request.getHeaders() != null) {
                request.getHeaders().forEach(builder::header);
            }
            HttpRequest.BodyPublisher body = request.getBody() == null || request.getBody().isEmpty()
                    ? HttpRequest.BodyPublishers.noBody()
                    : HttpRequest.BodyPublishers.ofString(request.getBody());
            String method = request.getMethod() == null ? "GET" : request.getMethod().toUpperCase();
            builder.method(method, body);
            return builder.build();
        }
    }

    /** 单档压测结果汇总 */
    private static class StageOutcome {
        private int concurrency;
        private long total;
        private long errors;
        private double qps;
        private double p50;
        private double p95;
        private double p99;
        private double max;
        private double avg;
        private double errorRate;
        private List<StressTaskResult.Sample> samples;

        private StressTaskResult.StagePoint toStagePoint() {
            StressTaskResult.StagePoint p = new StressTaskResult.StagePoint();
            p.setConcurrency(concurrency);
            p.setTotalRequests(total);
            p.setQps(qps);
            p.setP50Ms(p50);
            p.setP95Ms(p95);
            p.setP99Ms(p99);
            p.setErrorRate(errorRate);
            return p;
        }
    }

    private static double round2(double v) {
        return Math.round(v * 100.0) / 100.0;
    }
}
