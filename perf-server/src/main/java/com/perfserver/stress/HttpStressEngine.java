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
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
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
        // 分档配置归一化：兼容前端提交的 stageConcurrencies 简写形式
        List<StressTaskRequest.Stage> stages = request.resolveStages();
        StressTaskResult result = new StressTaskResult();
        result.setEngineType("http");
        result.setTargetUrl(request.getTargetUrl());
        result.setDurationSec(request.getDurationSec());
        result.setWarmupSec(request.getWarmupSec());
        result.setRampUpSec(request.getRampUpSec());

        List<StressTaskResult.Sample> allSamples = new ArrayList<>();
        List<StressStatistician.StepStats> allStepStats = new ArrayList<>();
        /** 场景配置告警（跨档汇总后去重；单接口模式恒空） */
        List<String> allWarnings = new ArrayList<>();

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
            allStepStats.addAll(outcome.stepStats);
            allWarnings.addAll(outcome.warnings);
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
                allStepStats.addAll(o.stepStats);
                allWarnings.addAll(o.warnings);
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
        // 分步骤统计（多接口场景编排；单接口模式为空列表）
        result.setStepStats(mergeStepStats(allStepStats));
        // 配置告警：跨档出现的同一条只保留一次（LinkedHashSet 保序去重）
        result.setWarnings(new ArrayList<>(new java.util.LinkedHashSet<>(allWarnings)));
        return result;
    }

    /**
     * 合并多档的分散步骤统计（按步骤名聚合）。
     *
     * <p>各档时长可能不同，QPS 无法简单相加，故这里只聚合请求数/错误数/耗时/最大值，
     * 平均值由「总耗时 ÷ 总请求数」重算。场景压测通常为单档，本方法主要保证
     * 「场景 + 分档」组合下汇总仍正确。
     */
    private static List<StressStatistician.StepStats> mergeStepStats(
            List<StressStatistician.StepStats> inputs) {
        if (inputs.isEmpty()) {
            return List.of();
        }
        Map<String, StepAccumulator> acc = new LinkedHashMap<>();
        for (StressStatistician.StepStats s : inputs) {
            acc.computeIfAbsent(s.getStepName(), k -> new StepAccumulator())
                    .add(s.getTotalRequests(), s.getErrorCount(),
                            Math.round(s.getAvgMs() * s.getTotalRequests()), (long) s.getMaxMs(),
                            s.getRenderMiss());
        }
        List<StressStatistician.StepStats> merged = new ArrayList<>(acc.size());
        acc.forEach((name, a) -> merged.add(StressStatistician.StepStats.of(
                name, a.total, a.errors, a.costMs, a.maxMs, a.renderMiss)));
        merged.sort(Comparator.comparing(StressStatistician.StepStats::getStepName));
        return merged;
    }

    /** 分档合并用的轻量累加器 */
    private static final class StepAccumulator {
        private long total;
        private long errors;
        private long costMs;
        private long maxMs;
        /** 上下文引用未命中累计数（renderMiss 为纯计数，可跨档直接相加） */
        private long renderMiss;

        void add(long t, long e, long cost, long max, long miss) {
            total += t;
            errors += e;
            costMs += cost;
            maxMs = Math.max(maxMs, max);
            renderMiss += miss;
        }
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

        // 场景编排：步骤选择器 + 任务级变量池（多 worker 共享，均为线程安全结构）
        Scenario scenario = request.isScenarioMode() ? Scenario.of(request) : null;

        ExecutorService pool = Executors.newFixedThreadPool(concurrency);
        HttpClient client = buildClient();
        long rampUpMs = request.getRampUpSec() * 1000L;
        for (int i = 0; i < concurrency; i++) {
            // 爬坡：worker i 在 [stageStart, stageStart+rampUpMs] 内线性依次激活
            long activateAt = rampUpMs > 0 ? stageStart + rampUpMs * i / concurrency : stageStart;
            pool.submit(new Worker(client, request, stop, stats, warmupDone, limiter, activateAt, scenario));
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
        outcome.stepStats = stats.stepStats(formalElapsedSec);
        // 分档模式下每档重建 Scenario（变量池与上下文池按档重置，与现状一致），
        // 告警内容逐档相同，由 run() 去重后统一写入结果
        outcome.warnings = scenario == null ? List.of() : scenario.warnings();
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
        /** 多接口场景（单接口模式为 null） */
        private final Scenario scenario;
        /**
         * 上下文引用未命中累加器（每 worker 一个，全程复用以免热路径分配）。
         *
         * <p>每次迭代渲染前清零、渲染后取走（见 {@link #runScenario()}）——
         * 不能让它跨迭代累积，否则 warmup/stop 期的未命中会被正式期首次统计一并提交。
         */
        private final long[] renderMiss = new long[1];
        /** 请求构造失败是否已打过日志（避免非法 URL 场景下每迭代刷屏） */
        private boolean buildFailureLogged;

        Worker(HttpClient client, StressTaskRequest request, AtomicBoolean stop,
               StressStatistician stats, AtomicBoolean warmupDone, QpsLimiter limiter, long activateAt,
               Scenario scenario) {
            this.client = client;
            this.request = request;
            this.stop = stop;
            this.stats = stats;
            this.warmupDone = warmupDone;
            this.limiter = limiter;
            this.activateAt = activateAt;
            this.scenario = scenario;
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
                if (scenario == null) {
                    runSingle();
                } else {
                    runScenario();
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }

        /** 单接口模式：预构建一次请求复用（与历史行为一致） */
        private void runSingle() throws InterruptedException {
            HttpRequest requestObj = buildRequest(request.getTargetUrl(), request.getMethod(),
                    request.getHeaders(), request.getBody());
            List<StressTaskRequest.Assertion> rules = request.getAssertions();
            // 无 body 断言时不读响应体（避免无谓的字符串驻留开销）
            boolean assertNeedsBody = ResponseAssertor.needsBody(rules);
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
                    } else if (rules != null && !rules.isEmpty()) {
                        // 状态码已通过，再按结果判断校验业务成败（如 200 包裹的 code=500）
                        error = !ResponseAssertor.matches(rules, resp.statusCode(),
                                resp.headers().map(), assertNeedsBody ? resp.body() : null);
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
        }

        /**
         * 多接口场景模式：每次迭代按权重选一个步骤，用「扁平变量池 + 步骤上下文池」
         * 双池渲染后发送；响应先原子写入本步骤的上下文槽位，再按声明的提取规则
         * 把值写回变量池（供后续步骤使用）。
         */
        private void runScenario() throws InterruptedException {
            while (!stop.get()) {
                if (limiter != null && !limiter.acquire(stop)) {
                    return;
                }
                int stepIdx = scenario.select();
                if (stepIdx < 0) {
                    // 全部步骤权重 <= 0：无可发步骤，直接结束该 worker
                    return;
                }
                StressTaskRequest.ScenarioStep step = scenario.stepAt(stepIdx);
                // 本迭代的上下文引用未命中数清零起点：渲染（url / headers / body）累计到 renderMiss[0]
                renderMiss[0] = 0;
                HttpRequest requestObj;
                try {
                    requestObj = buildRequest(
                            TemplateRenderer.render(step.getUrl(), scenario.pool(), scenario.ctx(), renderMiss),
                            step.getMethod(),
                            step.getHeaders(),
                            TemplateRenderer.render(step.getBody(), scenario.pool(), scenario.ctx(), renderMiss));
                } catch (RuntimeException e) {
                    // 渲染后的 URL / 请求头 / 请求体都可能因配置错误而构造失败：URL 里残留
                    // 未命中的 {{p9.result.x}} 会因 '{' '}' 非法而让 URI.create 抛异常，
                    // 请求头名或值含非法字符同样会抛 IllegalArgumentException。
                    // 设计约定「未命中保留原文、不中断压测」，故一律计一次错误并跳过本次迭代，
                    // 而不是让 worker 线程当场死亡——后者会让压测静默少掉若干并发，
                    // 比 100% 错误率难排查得多。
                    // 注意这里捕获 RuntimeException 是刻意的：HttpClient 的构造校验抛的是
                    // 未受检异常，且配置错误的形态无法穷举，收窄到 IllegalArgumentException
                    // 会漏掉其他形状，进而重新引入 worker 静默死亡的问题。
                    if (!buildFailureLogged) {
                        buildFailureLogged = true;
                        log.warn("scenario step {} request build failed (check placeholder config): {}",
                                stepIdx, e.toString());
                    }
                    long miss = renderMiss[0];
                    renderMiss[0] = 0;
                    if (warmupDone.get() && !stop.get()) {
                        stats.record(0, true, scenario.stepName(stepIdx), miss);
                    }
                    continue;
                }
                List<StressTaskRequest.Assertion> rules = step.getAssertions();
                // 断言是否需要读响应体（body/json 断言）；结果复用于判定与取值
                boolean assertNeedsBody = ResponseAssertor.needsBody(rules);
                // 启动期预编译的捕获标志：被上下文引用 / 有 extract / 断言需要，才驻留响应
                boolean needBody = scenario.captureBody(stepIdx) || assertNeedsBody;
                boolean needHeaders = scenario.captureHeaders(stepIdx) || assertNeedsBody;
                long t0 = System.nanoTime();
                boolean error = false;
                String body = null;
                Map<String, List<String>> respHeaders = null;
                try {
                    HttpResponse<String> resp = client.send(requestObj, HttpResponse.BodyHandlers.ofString());
                    // 断言与本步捕获都需要头表时只拷贝一次
                    boolean needHeaderSnapshot = needHeaders || (rules != null && !rules.isEmpty());
                    Map<String, List<String>> headerSnapshot = needHeaderSnapshot ? resp.headers().map() : null;
                    if (resp.statusCode() >= 400) {
                        error = true;
                    } else if (rules != null && !rules.isEmpty()) {
                        // 状态码已通过，再按本步骤的结果判断校验业务成败
                        error = !ResponseAssertor.matches(rules, resp.statusCode(),
                                headerSnapshot, assertNeedsBody ? resp.body() : null);
                    }
                    if (needHeaders) {
                        respHeaders = headerSnapshot;
                    }
                    if (needBody) {
                        body = resp.body();
                    }
                    // 入池必须在 extract 之前（设计 2.5）；两者互不依赖，但保证下游
                    // 引用本步响应与引用本步提取变量得到的是同一份数据。
                    // 错误响应（status>=400 / 断言未过）照常入池：下游引用"最新可达数据"，
                    // 压测要压出真实错误，不静默跳过（设计 2.3.5）。
                    // 未被任何步骤引用的槽位不入池：省掉一次快照分配（预编译已保证无人读它）。
                    if (scenario.referenced(stepIdx)) {
                        scenario.ctx().put(stepIdx, respHeaders, body);
                    }
                } catch (Exception e) {
                    // 网络异常：没有响应对象，不入池——用空快照覆盖会丢掉旧的可达数据
                    error = true;
                }
                long costMs = (System.nanoTime() - t0) / 1_000_000;
                // 【评审 P1】未命中数必须"取走并清零"，而不是累加后随统计提交：
                // stats.record 仅在正式期调用，若把 warmup/stop 期的未命中留在累加器里，
                // 会被正式期第一次 record 一并提交，导致 renderMiss 虚高。
                long missCount = renderMiss[0];
                renderMiss[0] = 0;
                boolean counted = warmupDone.get() && !stop.get();
                if (counted) {
                    stats.record(costMs, error, scenario.stepName(stepIdx), missCount);
                }
                // 变量提取在计数之后：即便本次不计入统计，也应更新变量池，
                // 保证后续步骤拿到新鲜登录态（warmup 期同样需要建立有效 token）
                if (needBody) {
                    ResponseVarExtractor.extract(body, respHeaders, step.getExtract(),
                            scenario.pool(), MAX_EXTRACT_BODY_CHARS);
                }
            }
        }

        private HttpRequest buildRequest(String url, String method,
                                        Map<String, String> headers, String body) {
            HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(url));
            // 压测流量标记（默认注入；被测应用可据此路由影子表/隔离数据，用户显式设置不覆盖）
            if (headers == null || !headers.containsKey(TRAFFIC_MARK_HEADER)) {
                builder.header(TRAFFIC_MARK_HEADER, "1");
            }
            if (headers != null) {
                if (scenario != null) {
                    // 场景模式：头值与 url/body 走完全相同的双池渲染——
                    // 既支持 {{var}}（如 Authorization: Bearer {{token}}），
                    // 也支持 {{p1.result.data.token}} / {{p1.header.X-Request-Id}} 上下文引用。
                    // 头名本身不渲染，仅值参与（设计 2.2「渲染作用域」）。
                    headers.forEach((k, v) -> builder.header(k,
                            TemplateRenderer.render(v, scenario.pool(), scenario.ctx(), renderMiss)));
                } else {
                    headers.forEach(builder::header);
                }
            }
            HttpRequest.BodyPublisher publisher = body == null || body.isEmpty()
                    ? HttpRequest.BodyPublishers.noBody()
                    : HttpRequest.BodyPublishers.ofString(body);
            String m = method == null || method.isBlank() ? "GET" : method.toUpperCase();
            builder.method(m, publisher);
            return builder.build();
        }
    }

    /** 参与变量提取的响应体最大字符数（超长响应截断，避免压测机被大响应拖垮） */
    private static final int MAX_EXTRACT_BODY_CHARS = 64 * 1024;

    /**
     * 多接口场景运行时上下文：步骤选择器 + <b>扁平变量池 + 步骤上下文池</b>双池。
     *
     * <p>由所有 worker 共享：选择器无状态、变量池为并发容器、上下文池为原子槽位数组，
     * 均无锁访问（惰性 JSON 解析的锁粒度是单个槽位，且仅首次付费）。
     *
     * <p>构建期（{@link #of}）一次性完成三件事，运行期零解析开销：
     * 别名表校验、上下文引用预编译（哪些步骤的响应需要驻留）、配置告警收集。
     */
    private static final class Scenario {
        /** 步骤快照（不可变，避免运行期请求对象被改动导致索引错位） */
        private final List<StressTaskRequest.ScenarioStep> steps;
        private final StepSelector selector;
        private final Map<String, String> pool;
        /** 步骤下标 → 展示名（未命名步骤按序号生成） */
        private final String[] stepNames;
        /** 步骤上下文池：每步最近一次响应的 header / body */
        private final StepContextStore ctxStore;
        /** 预编译：该步响应是否需要驻留 body（被 result 引用 / 有 extract / 有 body|json 断言） */
        private final boolean[] captureBody;
        /** 预编译：该步响应是否需要驻留 header（被 header 引用 / 有 extract / 有断言） */
        private final boolean[] captureHeaders;
        /**
         * 预编译：该槽位是否被<b>任一</b>步骤模板（url / 请求头值 / body）引用。
         *
         * <p>与 capture* 的区别：extract / 断言也会让响应被驻留，但那是给本步自己用的，
         * 不进上下文池。只有被引用（会有人读）的槽位才需要 {@code ctxStore.put}。
         */
        private final boolean[] referenced;
        /** 启动期配置告警（不 fail-fast：压测配置常由 AI 生成，宽松处理更可用） */
        private final List<String> warnings;

        private Scenario(List<StressTaskRequest.ScenarioStep> steps, StepSelector selector,
                         Map<String, String> pool, String[] stepNames, StepContextStore ctxStore,
                         boolean[] captureBody, boolean[] captureHeaders, boolean[] referenced,
                         List<String> warnings) {
            this.steps = steps;
            this.selector = selector;
            this.pool = pool;
            this.stepNames = stepNames;
            this.ctxStore = ctxStore;
            this.captureBody = captureBody;
            this.captureHeaders = captureHeaders;
            this.referenced = referenced;
            this.warnings = warnings;
        }

        static Scenario of(StressTaskRequest request) {
            List<StressTaskRequest.ScenarioStep> steps = List.copyOf(request.getScenario());
            String[] names = new String[steps.size()];
            for (int i = 0; i < steps.size(); i++) {
                String name = steps.get(i).getName();
                names[i] = (name == null || name.isBlank()) ? "step-" + (i + 1) : name;
            }
            List<String> warnings = new ArrayList<>();
            Map<String, Integer> aliasIndex = StepContextStore.buildAliasIndex(steps, warnings);
            StepContextStore ctxStore =
                    new StepContextStore(steps.size(), aliasIndex, MAX_EXTRACT_BODY_CHARS);

            // 预编译「哪些步骤的响应需要驻留」：先按 extract / 断言取基线，再叠加上下文引用
            boolean[] captureBody = new boolean[steps.size()];
            boolean[] captureHeaders = new boolean[steps.size()];
            boolean[] referenced = new boolean[steps.size()];
            for (int i = 0; i < steps.size(); i++) {
                StressTaskRequest.ScenarioStep s = steps.get(i);
                // 有 extract（可能取响应头）或 body|json 断言 → 头与体都要驻留（沿用原 needBody 语义）
                if (s != null && (hasExtract(s) || ResponseAssertor.needsBody(s.getAssertions()))) {
                    captureBody[i] = true;
                    captureHeaders[i] = true;
                }
            }
            collectContextRefs(steps, ctxStore, captureBody, captureHeaders, referenced, warnings);
            for (String w : warnings) {
                log.warn("scenario config warning: {}", w);
            }
            return new Scenario(steps, StepSelector.of(steps),
                    ResponseVarExtractor.createPool(request.getVariables()), names,
                    ctxStore, captureBody, captureHeaders, referenced, warnings);
        }

        /**
         * 扫描全部步骤模板（url / 请求头值 / 请求体）中的上下文引用，标记被引用的槽位。
         *
         * <p><b>为什么必须预编译</b>：运行期若对所有步骤无条件驻留响应体，场景压测会凭空多出
         * 「步骤数 × 64KB」的字符串驻留开销，而实际上只有被引用的步骤才需要。
         *
         * <p><b>成本澄清</b>：capture 标志只控制「是否驻留」，不控制「是否拷贝」——
         * 只要该步骤声明了断言，{@code resp.headers().map()} 的拷贝照常发生（断言传参需要）。
         * 故 {@code captureHeaders=false} 不等于零开销，勿据此误判优化收益。
         */
        private static void collectContextRefs(List<StressTaskRequest.ScenarioStep> steps,
                                               StepContextStore ctxStore,
                                               boolean[] captureBody, boolean[] captureHeaders,
                                               boolean[] referenced, List<String> warnings) {
            // 同一条越界告警可能在多个模板里重复出现，先去重再并入 warnings
            java.util.Set<String> scanWarnings = new java.util.LinkedHashSet<>();
            TemplateRenderer.RefSink sink = new TemplateRenderer.RefSink() {
                @Override
                public void accept(int slotIdx, boolean header) {
                    referenced[slotIdx] = true;
                    if (header) {
                        captureHeaders[slotIdx] = true;
                    } else {
                        captureBody[slotIdx] = true;
                    }
                }

                @Override
                public void outOfRange(String stepRef) {
                    scanWarnings.add("占位符 {{" + stepRef
                            + ".…}} 的步骤序号越界（共 " + steps.size() + " 步），渲染时将保留原文");
                }
            };
            for (StressTaskRequest.ScenarioStep step : steps) {
                if (step == null) {
                    continue;
                }
                TemplateRenderer.scanContextRefs(step.getUrl(), ctxStore, sink);
                if (step.getHeaders() != null) {
                    step.getHeaders().values().forEach(v -> TemplateRenderer.scanContextRefs(v, ctxStore, sink));
                }
                TemplateRenderer.scanContextRefs(step.getBody(), ctxStore, sink);
            }
            warnings.addAll(scanWarnings);
        }

        /**
         * 按权重随机选中一个步骤下标（O(log n)）。
         *
         * @return 步骤下标；全部步骤权重 <= 0 时返回 -1
         */
        int select() {
            return selector.select();
        }

        StressTaskRequest.ScenarioStep stepAt(int idx) {
            return steps.get(idx);
        }

        /** 步骤名（按下标直接取，避免热路径上的身份查找） */
        String stepName(int idx) {
            return stepNames[idx];
        }

        Map<String, String> pool() {
            return pool;
        }

        /** 步骤上下文池（双池渲染的上下文侧） */
        StepContextStore ctx() {
            return ctxStore;
        }

        /** 该步响应是否需要驻留 body（预编译结果） */
        boolean captureBody(int stepIdx) {
            return captureBody[stepIdx];
        }

        /** 该步响应是否需要驻留 header（预编译结果） */
        boolean captureHeaders(int stepIdx) {
            return captureHeaders[stepIdx];
        }

        /** 该槽位是否被任一步骤模板引用（无人引用的槽位不必写上下文池） */
        boolean referenced(int stepIdx) {
            return referenced[stepIdx];
        }

        /** 启动期配置告警 */
        List<String> warnings() {
            return warnings;
        }
    }

    /** 步骤是否声明了变量提取规则 */
    private static boolean hasExtract(StressTaskRequest.ScenarioStep step) {
        return step.getExtract() != null && !step.getExtract().isEmpty();
    }

    /** 单档压测结果汇总 */
    private static class StageOutcome {
        /** 分步骤统计（多接口场景；单接口为空列表） */
        private List<StressStatistician.StepStats> stepStats = List.of();
        /** 场景配置告警（由 run() 跨档去重汇总后写入结果） */
        private List<String> warnings = List.of();
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
