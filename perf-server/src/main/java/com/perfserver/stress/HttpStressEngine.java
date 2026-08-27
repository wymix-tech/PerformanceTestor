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

/**
 * HTTP 压测引擎：有界线程池并发发压 + 周期采样（实时图表）+ 分档并发（容量曲线）。
 *
 * <p>统计 O(1) 内存（固定桶直方图）；采样线程与业务线程分离，不阻塞发压主循环。
 */
@Component
public class HttpStressEngine implements StressEngine {

    private static final Logger log = LoggerFactory.getLogger(HttpStressEngine.class);

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

        List<StressTaskResult.Sample> allSamples = new ArrayList<>();

        if (stages == null || stages.isEmpty()) {
            // 单档模式
            StressTaskRequest.Stage single = new StressTaskRequest.Stage();
            single.setConcurrency(request.getConcurrency());
            single.setDurationSec(request.getDurationSec());
            StageOutcome outcome = runStage(request, single, listener);
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
            // 分档模式（容量评估）
            long total = 0;
            long errors = 0;
            List<StressTaskResult.StagePoint> points = new ArrayList<>();
            for (StressTaskRequest.Stage stage : stages) {
                log.info("stress stage: concurrency={}, duration={}s", stage.getConcurrency(), stage.getDurationSec());
                StageOutcome o = runStage(request, stage, listener);
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

    /** 执行单档压测 */
    private StageOutcome runStage(StressTaskRequest request,
                                  StressTaskRequest.Stage stage,
                                  StressSampleListener listener) {
        StressStatistician stats = new StressStatistician();
        AtomicBoolean stop = new AtomicBoolean(false);
        int concurrency = stage.getConcurrency();
        int durationSec = stage.getDurationSec();
        long stageStart = System.currentTimeMillis();

        ExecutorService pool = Executors.newFixedThreadPool(concurrency);
        HttpClient client = buildClient();
        for (int i = 0; i < concurrency; i++) {
            pool.submit(new Worker(client, request, stop, stats));
        }

        // 周期采样：实时图表数据 + 落库回调
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
        }, intervalMs, intervalMs, TimeUnit.MILLISECONDS);

        // 等待时长到点后停止发压
        long deadline = stageStart + durationSec * 1000L;
        while (System.currentTimeMillis() < deadline) {
            try {
                Thread.sleep(50);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
        }
        stop.set(true);
        sampler.shutdown();
        pool.shutdownNow();
        try {
            pool.awaitTermination(10, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }

        // 阶段累计统计（整体分位数基于阶段累计直方图）
        StressStatistician.StageSnapshot snap = stats.stageSnapshot();
        double elapsedSec = Math.max(1, (System.currentTimeMillis() - stageStart) / 1000.0);
        StageOutcome outcome = new StageOutcome();
        outcome.total = snap.getTotal();
        outcome.errors = snap.getErrorCount();
        outcome.qps = round2(outcome.total / elapsedSec);
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

    /** 压测工作线程：持续发压直到 stop 标志置位 */
    private static class Worker implements Runnable {
        private final HttpClient client;
        private final StressTaskRequest request;
        private final AtomicBoolean stop;
        private final StressStatistician stats;
        /** QPS 限流：每个 worker 请求间隔 ms（0 表示不限） */
        private final long throttleMs;

        Worker(HttpClient client, StressTaskRequest request, AtomicBoolean stop, StressStatistician stats) {
            this.client = client;
            this.request = request;
            this.stop = stop;
            this.stats = stats;
            this.throttleMs = request.getQpsLimit() > 0
                    ? Math.max(1, 1000L * request.getConcurrency() / request.getQpsLimit())
                    : 0;
        }

        @Override
        public void run() {
            HttpRequest requestObj = buildRequest();
            while (!stop.get()) {
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
                stats.record(costMs, error);
                if (throttleMs > 0) {
                    try {
                        Thread.sleep(throttleMs);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        return;
                    }
                }
            }
        }

        private HttpRequest buildRequest() {
            HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(request.getTargetUrl()));
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
