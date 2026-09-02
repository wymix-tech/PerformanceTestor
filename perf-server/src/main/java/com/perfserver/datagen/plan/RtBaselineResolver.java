package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.StressSample;
import com.perfserver.entity.StressTask;
import com.perfserver.mapper.StressSampleMapper;
import com.perfserver.mapper.StressTaskMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Locale;

/**
 * 响应时间基线解析器：为行数推导提供 assumedRtMs。
 *
 * <p>固定的 50ms 默认值在慢接口上会让请求量被高估几十倍，进而把行数推到失真的量级。
 * 故按三级取值，取值来源写入方案 {@code rtSource} 并在前端方案卡标注，
 * 避免用户误以为行数都是精算值：</p>
 * <ol>
 *   <li>{@code history}：同场景历史压测的 avgMs（退化取 p95Ms）</li>
 *   <li>{@code sample}：该任务采样点的 p50 均值</li>
 *   <li>{@code default}：缺省 {@value #DEFAULT_RT_MS}，clamp[{@value #MIN_RT_MS},{@value #MAX_RT_MS}]</li>
 * </ol>
 *
 * <p>任一级取数失败静默降级到下一级，不中断分析。</p>
 */
@Component
public class RtBaselineResolver {

    private static final Logger log = LoggerFactory.getLogger(RtBaselineResolver.class);

    public static final double DEFAULT_RT_MS = 50.0;
    public static final double MIN_RT_MS = 5.0;
    public static final double MAX_RT_MS = 5000.0;

    /** 取值来源标签 */
    public static final String SRC_HISTORY = "history";
    public static final String SRC_SAMPLE = "sample";
    public static final String SRC_DEFAULT = "default";

    /** 采样点均值参与计算的点数 */
    private static final int SAMPLE_LOOKBACK = 20;

    private final StressTaskMapper taskMapper;
    private final StressSampleMapper sampleMapper;
    private final ObjectMapper objectMapper;

    public RtBaselineResolver(StressTaskMapper taskMapper, StressSampleMapper sampleMapper,
                              ObjectMapper objectMapper) {
        this.taskMapper = taskMapper;
        this.sampleMapper = sampleMapper;
        this.objectMapper = objectMapper;
    }

    /** 基线结果 */
    public static final class Baseline {
        private final double rtMs;
        private final String source;

        Baseline(double rtMs, String source) {
            this.rtMs = rtMs;
            this.source = source;
        }

        public double getRtMs() {
            return rtMs;
        }

        public String getSource() {
            return source;
        }
    }

    /**
     * 解析 RT 基线。
     *
     * @param appName    应用名（可空）
     * @param targetUrl  目标 URL（可空）
     * @param taskId     压测任务 ID（可空；提供时优先取该任务采样点）
     * @return 基线（恒非空，最差返回缺省值）
     */
    public Baseline resolve(String appName, String targetUrl, Long taskId) {
        if (taskId != null) {
            Baseline fromSamples = fromSamples(taskId);
            if (fromSamples != null) {
                return fromSamples;
            }
        }
        Baseline fromHistory = fromHistory(appName, targetUrl);
        if (fromHistory != null) {
            return fromHistory;
        }
        return new Baseline(DEFAULT_RT_MS, SRC_DEFAULT);
    }

    /** 采样点均值（p50 优先，缺失时取 p95） */
    private Baseline fromSamples(long taskId) {
        try {
            List<StressSample> samples = sampleMapper.queryByTask(taskId, SAMPLE_LOOKBACK);
            if (samples == null || samples.isEmpty()) {
                return null;
            }
            double sum = 0;
            int n = 0;
            for (StressSample s : samples) {
                Double v = s.getP50Ms() != null && s.getP50Ms() > 0 ? s.getP50Ms() : s.getP95Ms();
                if (v != null && v > 0) {
                    sum += v;
                    n++;
                }
            }
            if (n == 0) {
                return null;
            }
            return new Baseline(clamp(sum / n), SRC_SAMPLE);
        } catch (Exception e) {
            log.debug("rt baseline from samples failed, taskId={}: {}", taskId, e.getMessage());
            return null;
        }
    }

    /** 同场景历史压测：task.resultJson 的 avgMs（退化 p95Ms） */
    private Baseline fromHistory(String appName, String targetUrl) {
        if (appName == null || appName.isBlank() || targetUrl == null || targetUrl.isBlank()) {
            return null;
        }
        try {
            List<StressTask> history = taskMapper.queryByScene(appName, targetUrl, "finished", 1);
            if (history == null || history.isEmpty()) {
                return null;
            }
            double rt = parseRt(history.get(0).getResultJson());
            return rt > 0 ? new Baseline(clamp(rt), SRC_HISTORY) : null;
        } catch (Exception e) {
            log.debug("rt baseline from history failed: {}", e.getMessage());
            return null;
        }
    }

    /** 解析 resultJson 的 avgMs，缺失/异常时退化取 p95Ms */
    double parseRt(String resultJson) {
        if (resultJson == null || resultJson.isBlank()) {
            return 0;
        }
        try {
            JsonNode root = objectMapper.readTree(resultJson);
            double avg = firstPositive(root, "avgMs");
            if (avg > 0) {
                return avg;
            }
            return firstPositive(root, "p95Ms");
        } catch (Exception e) {
            log.debug("parse resultJson for rt failed: {}", e.getMessage());
            return 0;
        }
    }

    /** 先在根节点找，再在 stats/summary 子节点找（历史 JSON 结构可能嵌套） */
    private double firstPositive(JsonNode root, String field) {
        JsonNode direct = root.get(field);
        if (direct != null && direct.isNumber() && direct.asDouble() > 0) {
            return direct.asDouble();
        }
        for (String wrapper : new String[]{"stats", "summary", "result"}) {
            JsonNode child = root.get(wrapper);
            if (child != null && child.isObject()) {
                JsonNode v = child.get(field);
                if (v != null && v.isNumber() && v.asDouble() > 0) {
                    return v.asDouble();
                }
            }
        }
        return 0;
    }

    static double clamp(double rt) {
        if (Double.isNaN(rt) || Double.isInfinite(rt)) {
            return DEFAULT_RT_MS;
        }
        return Math.max(MIN_RT_MS, Math.min(MAX_RT_MS, rt));
    }

    /** 来源标签的中文说明（前端方案卡展示） */
    public static String describe(String source) {
        if (source == null) {
            return "默认 50ms";
        }
        switch (source.toLowerCase(Locale.ROOT)) {
            case SRC_HISTORY:
                return "历史压测";
            case SRC_SAMPLE:
                return "采样点";
            default:
                return "默认 50ms";
        }
    }
}
