package com.perfserver.stress;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.BufferedReader;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.TimeUnit;

/**
 * JMeter 压测引擎：生成 JMX 计划 → CLI 调用（零 Maven 依赖）→ 解析 JTL 重建统计。
 *
 * <p>相比内置 http 引擎的价值：真实 JMeter 生态（思考时间/断言/多接口混合场景可自行扩展 JMX 模板），
 * 统计口径与 http 引擎一致（分位/错误率/QPS 曲线），结果互通。
 *
 * <p>降级策略：{@link #available()} 检测 JMeter 安装（jmeterHome 配置或 PATH），
 * 未安装时任务创建即被拒绝并给出安装指引，不影响默认 http 引擎。
 *
 * <p>映射关系：concurrency → ThreadGroup.num_threads；durationSec → ThreadGroup.duration；
 * rampUpSec → ThreadGroup.ramp_time；qpsLimit → ConstantThroughputTimer（每分钟吞吐）；
 * warmupSec → JTL 解析时按 timeStamp 过滤（发压但不计入统计）。
 */
@Component
public class JmeterStressEngine implements StressEngine {

    private static final Logger log = LoggerFactory.getLogger(JmeterStressEngine.class);

    /** JMeter 安装目录（bin/jmeter 的父目录）；空则尝试 PATH */
    @Value("${perf.stress.jmeter-home:}")
    private String jmeterHome;

    /** 可用性检测缓存（null 未检测） */
    private volatile Boolean availableCache;

    @Override
    public String type() {
        return "jmeter";
    }

    /** JMeter 是否可用（jmeterHome/bin/jmeter 存在，或 PATH 上有 jmeter） */
    @Override
    public boolean available() {
        Boolean cached = availableCache;
        if (cached != null) {
            return cached;
        }
        boolean ok = detect();
        availableCache = ok;
        return ok;
    }

    private boolean detect() {
        try {
            Path bin = jmeterExecutable();
            if (bin != null) {
                return true;
            }
            // PATH 探测：jmeter --version 退出码 0 视为可用（仅检测一次并缓存）
            Process p = new ProcessBuilder("jmeter", "--version")
                    .redirectErrorStream(true).start();
            boolean finished = p.waitFor(30, TimeUnit.SECONDS);
            return finished && p.exitValue() == 0;
        } catch (Exception e) {
            return false;
        }
    }

    /** jmeterHome 显式配置时返回 bin/jmeter 可执行文件路径 */
    private Path jmeterExecutable() {
        if (jmeterHome == null || jmeterHome.isBlank()) {
            return null;
        }
        Path bin = Path.of(jmeterHome.trim(), "bin", "jmeter");
        if (Files.isExecutable(bin)) {
            return bin;
        }
        Path bat = Path.of(jmeterHome.trim(), "bin", "jmeter.bat");
        return Files.isExecutable(bat) ? bat : null;
    }

    @Override
    public StressTaskResult run(StressTaskRequest request, StressSampleListener listener) {
        if (!available()) {
            throw new UnsupportedOperationException(
                    "JMeter 引擎不可用：请配置 perf.stress.jmeter-home（或环境变量 JMETER_HOME）指向 JMeter 安装目录，"
                            + "或将 jmeter 加入 PATH。未安装时可使用默认 http 引擎。");
        }

        List<StressTaskRequest.Stage> stages = request.getStages();
        StressTaskResult result = new StressTaskResult();
        result.setEngineType("jmeter");
        result.setTargetUrl(request.getTargetUrl());
        result.setDurationSec(request.getDurationSec());
        result.setWarmupSec(request.getWarmupSec());
        result.setRampUpSec(request.getRampUpSec());

        if (stages == null || stages.isEmpty()) {
            StressTaskRequest.Stage single = new StressTaskRequest.Stage();
            single.setConcurrency(request.getConcurrency());
            single.setDurationSec(request.getDurationSec());
            fillSingle(result, runSingle(request, single, listener));
        } else {
            long total = 0;
            long errors = 0;
            List<StressTaskResult.Sample> allSamples = new ArrayList<>();
            List<StressTaskResult.StagePoint> points = new ArrayList<>();
            for (int i = 0; i < stages.size(); i++) {
                StressTaskRequest.Stage stage = stages.get(i);
                log.info("jmeter stage: concurrency={}, duration={}s", stage.getConcurrency(), stage.getDurationSec());
                StageOutcome o = runSingle(request, stage, listener);
                points.add(o.toPoint(stage.getConcurrency()));
                total += o.total;
                errors += o.errors;
                allSamples.addAll(o.samples);
            }
            result.setConcurrency(request.getConcurrency());
            result.setTotalRequests(total);
            result.setErrorCount(errors);
            result.setErrorRate(total == 0 ? 0 : round2(errors * 100.0 / total));
            result.setSamples(allSamples);
            result.setStagePoints(points);
        }
        return result;
    }

    private void fillSingle(StressTaskResult result, StageOutcome o) {
        result.setConcurrency(o.concurrency);
        result.setTotalRequests(o.total);
        result.setErrorCount(o.errors);
        result.setErrorRate(o.errorRate);
        result.setQps(o.qps);
        result.setP50Ms(o.p50);
        result.setP95Ms(o.p95);
        result.setP99Ms(o.p99);
        result.setMaxMs(o.max);
        result.setAvgMs(o.avg);
        result.setSamples(o.samples);
        result.setStagePoints(List.of(o.toPoint(o.concurrency)));
    }

    /** 执行单档：生成 JMX → jmeter -n → 解析 JTL（warmup 按 timeStamp 过滤） */
    private StageOutcome runSingle(StressTaskRequest request,
                                   StressTaskRequest.Stage stage,
                                   StressSampleListener listener) {
        Path workDir = null;
        try {
            workDir = Files.createTempDirectory("perf-jmeter-");
            Path jmx = workDir.resolve("plan.jmx");
            Path jtl = workDir.resolve("result.jtl");
            Path jmeterLog = workDir.resolve("jmeter.log");
            Files.writeString(jmx, buildJmx(request, stage), StandardCharsets.UTF_8);

            long startMs = System.currentTimeMillis();
            ProcessBuilder pb = new ProcessBuilder(
                    jmeterExecutable() == null ? "jmeter" : jmeterExecutable().toString(),
                    "-n", "-t", jmx.toString(), "-l", jtl.toString(), "-j", jmeterLog.toString());
            pb.redirectErrorStream(true);
            Process process = pb.start();
            // 后台消费输出防管道阻塞
            Thread outputDrain = new Thread(() -> {
                try (BufferedReader reader = process.inputReader()) {
                    String line;
                    while ((line = reader.readLine()) != null) {
                        log.debug("[jmeter] {}", line);
                    }
                } catch (IOException ignored) {
                    // 进程结束期读异常忽略
                }
            }, "jmeter-output");
            outputDrain.setDaemon(true);
            outputDrain.start();

            // 超时保护：任务时长 + ramp + warmup + 5 分钟宽限
            long timeoutSec = request.getWarmupSec() + request.getRampUpSec()
                    + stage.getDurationSec() + 300L;
            boolean finished = process.waitFor(timeoutSec, TimeUnit.SECONDS);
            if (!finished) {
                process.destroyForcibly();
                throw new IllegalStateException("JMeter 进程超时未退出（" + timeoutSec + "s），已强制终止");
            }
            if (process.exitValue() != 0) {
                throw new IllegalStateException("JMeter 进程退出码 " + process.exitValue()
                        + "，详见日志（jmeterHome/日志文件或 DEBUG 输出）");
            }
            long endMs = System.currentTimeMillis();

            // warmup 过滤基准：进程启动 + warmup 秒（JMeter 自身启动开销约 2-5s 已被 warmup 覆盖）
            long formalStartMs = startMs + request.getWarmupSec() * 1000L;
            return parseJtl(jtl, formalStartMs, stage.getConcurrency(), listener, endMs);
        } catch (IOException | InterruptedException e) {
            if (e instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            throw new IllegalStateException("JMeter 引擎执行失败: " + e.getMessage(), e);
        } finally {
            cleanup(workDir);
        }
    }

    // ===================== JMX 生成 =====================

    /** 生成最小可用 JMX 测试计划（ThreadGroup + HeaderManager + HTTPSampler + 可选吞吐定时器） */
    String buildJmx(StressTaskRequest request, StressTaskRequest.Stage stage) {
        String url = request.getTargetUrl();
        String scheme = "http";
        String host = "";
        String port = "";
        String path = "/";
        try {
            java.net.URI uri = java.net.URI.create(url);
            scheme = uri.getScheme() == null ? "http" : uri.getScheme();
            host = uri.getHost() == null ? "" : uri.getHost();
            port = uri.getPort() == -1 ? "" : String.valueOf(uri.getPort());
            path = uri.getRawPath() == null || uri.getRawPath().isEmpty() ? "/" : uri.getRawPath()
                    + (uri.getRawQuery() == null ? "" : "?" + uri.getRawQuery());
        } catch (Exception e) {
            throw new IllegalArgumentException("目标 URL 解析失败: " + url);
        }
        int concurrency = stage.getConcurrency();
        int duration = stage.getDurationSec();
        int rampUp = Math.max(request.getRampUpSec(), 1); // JMeter ramp_time 最小 1s

        // 压测流量标记头（用户未显式设置时注入，与 http 引擎行为一致）
        java.util.Map<String, String> headers = request.getHeaders() == null
                ? new java.util.LinkedHashMap<>() : new java.util.LinkedHashMap<>(request.getHeaders());
        headers.putIfAbsent(HttpStressEngine.TRAFFIC_MARK_HEADER, "1");

        StringBuilder sb = new StringBuilder(4096);
        sb.append("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
        sb.append("<jmeterTestPlan version=\"1.2\" properties=\"5.0\" jmeter=\"5.6\">\n");
        sb.append("  <hashTree>\n");
        // ThreadGroup
        sb.append("    <ThreadGroup guiclass=\"ThreadGroupGui\" testclass=\"ThreadGroup\" testname=\"perf-threadgroup\">\n");
        sb.append("      <stringProp name=\"ThreadGroup.num_threads\">").append(concurrency).append("</stringProp>\n");
        sb.append("      <stringProp name=\"ThreadGroup.ramp_time\">").append(rampUp).append("</stringProp>\n");
        sb.append("      <boolProp name=\"ThreadGroup.scheduler\">true</boolProp>\n");
        sb.append("      <stringProp name=\"ThreadGroup.duration\">").append(duration).append("</stringProp>\n");
        sb.append("      <stringProp name=\"ThreadGroup.on_sample_error\">continue</stringProp>\n");
        sb.append("      <elementProp name=\"ThreadGroup.main_controller\" elementType=\"LoopController\" guiclass=\"LoopControlPanel\" testclass=\"LoopController\">\n");
        sb.append("        <boolProp name=\"LoopController.continue_forever\">false</boolProp>\n");
        sb.append("        <stringProp name=\"LoopController.loops\">-1</stringProp>\n");
        sb.append("      </elementProp>\n");
        sb.append("    </ThreadGroup>\n");
        sb.append("    <hashTree>\n");
        // HeaderManager（自定义请求头 + 流量标记）
        if (!headers.isEmpty()) {
            sb.append("      <HeaderManager guiclass=\"HeaderPanel\" testclass=\"HeaderManager\" testname=\"perf-headers\">\n");
            sb.append("        <collectionProp name=\"HeaderManager.headers\">\n");
            headers.forEach((k, v) -> {
                sb.append("          <elementProp name=\"\" elementType=\"Header\">\n");
                sb.append("            <stringProp name=\"Header.name\">").append(esc(k)).append("</stringProp>\n");
                sb.append("            <stringProp name=\"Header.value\">").append(esc(v)).append("</stringProp>\n");
                sb.append("          </elementProp>\n");
            });
            sb.append("        </collectionProp>\n");
            sb.append("      </HeaderManager>\n");
            sb.append("      <hashTree/>\n");
        }
        // HTTPSamplerProxy
        String method = request.getMethod() == null ? "GET" : request.getMethod().toUpperCase();
        String body = request.getBody() == null ? "" : request.getBody();
        sb.append("      <HTTPSamplerProxy guiclass=\"HttpTestSampleGui\" testclass=\"HTTPSamplerProxy\" testname=\"perf-request\">\n");
        sb.append("        <elementProp name=\"HTTPsampler.Arguments\" elementType=\"Arguments\">\n");
        if (!body.isBlank() && !"GET".equals(method)) {
            sb.append("          <collectionProp name=\"Arguments.arguments\">\n");
            sb.append("            <elementProp name=\"\" elementType=\"HTTPArgument\">\n");
            sb.append("              <boolProp name=\"HTTPArgument.always_encode\">false</boolProp>\n");
            sb.append("              <stringProp name=\"Argument.value\">").append(esc(body)).append("</stringProp>\n");
            sb.append("              <stringProp name=\"Argument.metadata\">=</stringProp>\n");
            sb.append("            </elementProp>\n");
            sb.append("          </collectionProp>\n");
        } else {
            sb.append("          <collectionProp name=\"Arguments.arguments\"/>\n");
        }
        sb.append("        </elementProp>\n");
        if (!body.isBlank() && !"GET".equals(method)) {
            sb.append("        <boolProp name=\"HTTPSampler.postBodyRaw\">true</boolProp>\n");
        }
        sb.append("        <stringProp name=\"HTTPSampler.domain\">").append(esc(host)).append("</stringProp>\n");
        sb.append("        <stringProp name=\"HTTPSampler.port\">").append(esc(port)).append("</stringProp>\n");
        sb.append("        <stringProp name=\"HTTPSampler.protocol\">").append(esc(scheme)).append("</stringProp>\n");
        sb.append("        <stringProp name=\"HTTPSampler.path\">").append(esc(path)).append("</stringProp>\n");
        sb.append("        <stringProp name=\"HTTPSampler.method\">").append(esc(method)).append("</stringProp>\n");
        sb.append("        <boolProp name=\"HTTPSampler.follow_redirects\">true</boolProp>\n");
        sb.append("        <boolProp name=\"HTTPSampler.auto_redirects\">false</boolProp>\n");
        sb.append("        <boolProp name=\"HTTPSampler.use_keepalive\">true</boolProp>\n");
        sb.append("      </HTTPSamplerProxy>\n");
        sb.append("      <hashTree/>\n");
        // ConstantThroughputTimer（QPS 限流；吞吐单位 = 分钟）
        if (request.getQpsLimit() > 0) {
            sb.append("      <ConstantThroughputTimer guiclass=\"TestBeanGUI\" testclass=\"ConstantThroughputTimer\" testname=\"perf-qps-limiter\">\n");
            sb.append("        <intProp name=\"calcMode\">2</intProp>\n"); // all active threads
            sb.append("        <stringProp name=\"throughput\">").append(request.getQpsLimit() * 60L).append("</stringProp>\n");
            sb.append("      </ConstantThroughputTimer>\n");
            sb.append("      <hashTree/>\n");
        }
        sb.append("    </hashTree>\n");
        sb.append("  </hashTree>\n");
        sb.append("</jmeterTestPlan>\n");
        return sb.toString();
    }

    private static String esc(String s) {
        if (s == null) {
            return "";
        }
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\"", "&quot;").replace("'", "&apos;");
    }

    // ===================== JTL 解析 =====================

    /**
     * 解析 JTL CSV（默认格式：timeStamp,elapsed,label,responseCode,responseMessage,
     * threadName,dataType,success,failureMessage,bytes,sentBytes,grpThreads,allThreads,
     * URL,Latency,IdleTime,Connect）。
     *
     * <p>timeStamp &lt; formalStartMs 的样本属 warmup 期，不计入统计；
     * 按秒分桶产出 QPS 曲线样本，统计口径与 http 引擎一致（分档容量曲线复用 StagePoint）。
     */
    private StageOutcome parseJtl(Path jtl, long formalStartMs, int concurrency,
                                  StressSampleListener listener, long endMs) throws IOException {
        // 秒 → 该秒样本（elapsed, error）聚合
        TreeMap<Long, List<long[]>> bySecond = new TreeMap<>();
        StressStatistician stats = new StressStatistician();
        long total = 0;
        long errors = 0;
        long totalCost = 0;
        if (Files.exists(jtl)) {
            try (BufferedReader reader = Files.newBufferedReader(jtl, StandardCharsets.UTF_8)) {
                String line = reader.readLine(); // 表头
                while ((line = reader.readLine()) != null) {
                    if (line.isBlank()) {
                        continue;
                    }
                    List<String> cols = splitCsv(line);
                    if (cols.size() < 8) {
                        continue;
                    }
                    try {
                        long ts = Long.parseLong(cols.get(0).trim());
                        long elapsed = Long.parseLong(cols.get(1).trim());
                        boolean ok = Boolean.parseBoolean(cols.get(7).trim());
                        if (ts < formalStartMs) {
                            continue; // warmup 期样本不计入统计
                        }
                        total++;
                        if (!ok) {
                            errors++;
                        }
                        totalCost += elapsed;
                        stats.record(elapsed, !ok);
                        bySecond.computeIfAbsent(ts / 1000, k -> new ArrayList<>())
                                .add(new long[]{elapsed, ok ? 0 : 1});
                    } catch (NumberFormatException ignored) {
                        // 非数据行（异常输出混入）跳过
                    }
                }
            }
        }

        // 秒级样本曲线
        List<StressTaskResult.Sample> samples = new ArrayList<>(bySecond.size());
        bySecond.forEach((sec, rows) -> {
            StressStatistician second = new StressStatistician();
            long errCount = 0;
            for (long[] r : rows) {
                second.record(r[0], r[1] == 1);
                if (r[1] == 1) {
                    errCount++;
                }
            }
            StressStatistician.StageSnapshot snap = second.stageSnapshot();
            StressTaskResult.Sample s = new StressTaskResult.Sample();
            s.setSampleTime(sec * 1000);
            s.setConcurrency(concurrency);
            s.setQps((double) snap.getTotal());
            s.setP50Ms(StressStatistician.percentile(snap.getHistogram(), 50));
            s.setP95Ms(StressStatistician.percentile(snap.getHistogram(), 95));
            s.setP99Ms(StressStatistician.percentile(snap.getHistogram(), 99));
            s.setErrorCount(errCount);
            samples.add(s);
            if (listener != null) {
                try {
                    listener.onSample(s);
                } catch (Exception e) {
                    log.warn("stress sample listener failed", e);
                }
            }
        });

        // 汇总
        StressStatistician.StageSnapshot snap = stats.stageSnapshot();
        double formalElapsedSec = Math.max(1.0, (endMs - formalStartMs) / 1000.0);
        StageOutcome o = new StageOutcome();
        o.concurrency = concurrency;
        o.total = total;
        o.errors = errors;
        o.qps = round2(total / formalElapsedSec);
        o.p50 = StressStatistician.percentile(snap.getHistogram(), 50);
        o.p95 = StressStatistician.percentile(snap.getHistogram(), 95);
        o.p99 = StressStatistician.percentile(snap.getHistogram(), 99);
        o.max = StressStatistician.max(snap.getHistogram());
        o.avg = round2(total == 0 ? 0 : (double) totalCost / total);
        o.errorRate = total == 0 ? 0 : round2(errors * 100.0 / total);
        o.samples = samples;
        log.info("jmeter stage parsed: total={}, errors={}, qps={}, p95={}ms", total, errors, o.qps, o.p95);
        return o;
    }

    /** 最小 CSV 切分（处理双引号包裹字段与转义） */
    static List<String> splitCsv(String line) {
        List<String> cols = new ArrayList<>();
        StringBuilder cur = new StringBuilder();
        boolean inQuotes = false;
        for (int i = 0; i < line.length(); i++) {
            char c = line.charAt(i);
            if (inQuotes) {
                if (c == '"') {
                    if (i + 1 < line.length() && line.charAt(i + 1) == '"') {
                        cur.append('"');
                        i++;
                    } else {
                        inQuotes = false;
                    }
                } else {
                    cur.append(c);
                }
            } else {
                if (c == '"') {
                    inQuotes = true;
                } else if (c == ',') {
                    cols.add(cur.toString());
                    cur.setLength(0);
                } else {
                    cur.append(c);
                }
            }
        }
        cols.add(cur.toString());
        return cols;
    }

    private void cleanup(Path workDir) {
        if (workDir == null) {
            return;
        }
        try {
            try (var stream = Files.list(workDir)) {
                stream.forEach(f -> {
                    try {
                        Files.deleteIfExists(f);
                    } catch (IOException ignored) {
                        // 临时文件删除失败无碍
                    }
                });
            }
            Files.deleteIfExists(workDir);
        } catch (IOException ignored) {
            // 临时目录清理失败无碍
        }
    }

    /** 单档结果（内部复用） */
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

        private StressTaskResult.StagePoint toPoint(int concurrency) {
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
