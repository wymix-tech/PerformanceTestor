package com.perfserver.stress;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 压测任务请求：单档并发或分档阶梯（stages）两种模式。
 *
 * <p>stages 模式用于容量评估：按多档并发依次执行，每档结束后生成一个采样点
 * （并发/QPS/RT 分位数），供容量曲线插值分析。
 */
public class StressTaskRequest {

    /** 任务名称（可选，默认取 URL） */
    private String taskName;
    /** 引擎类型：http / jmeter */
    private String engineType = "http";
    /** 压测目标 URL */
    private String targetUrl;
    /** 归属应用（agent 上报应用名，随任务落库，供问题关联） */
    private String appName;
    /** HTTP 方法（GET/POST/...） */
    private String method = "GET";
    /** 并发数（单档模式） */
    private int concurrency = 1;
    /** 持续时长秒（单档模式） */
    private int durationSec = 60;
    /** QPS 上限（0 不限制） */
    private int qpsLimit;
    /**
     * 预热时长秒（默认 10）：warmup 期发压但不计入正式统计——
     * 消除首波冷连接（TCP/TLS 握手、目标 JIT 未热）对 P99 的污染。设 0 关闭。
     */
    private int warmupSec = 10;
    /**
     * 并发爬坡时长秒（默认 0 即全量并发立即启动）：worker 按序号在爬坡期内
     * 依次激活（线性阶梯升并发），避免瞬时连接风暴。建议 ≤ warmupSec 以获得干净统计。
     */
    private int rampUpSec;
    /** 请求头 */
    private Map<String, String> headers = new HashMap<>();
    /** 请求体（POST 等） */
    private String body;
    /** 分档并发模式（容量评估）：每档 {concurrency, durationSec} */
    private List<Stage> stages;
    /** 采样周期秒（默认 5，用于实时图表） */
    private int sampleIntervalSec = 5;

    /** 分档配置 */
    public static class Stage {
        private int concurrency;
        private int durationSec;

        public int getConcurrency() {
            return concurrency;
        }

        public void setConcurrency(int concurrency) {
            this.concurrency = concurrency;
        }

        public int getDurationSec() {
            return durationSec;
        }

        public void setDurationSec(int durationSec) {
            this.durationSec = durationSec;
        }
    }

    /** 便捷构造单档请求 */
    public static StressTaskRequest single(String url, int concurrency, int durationSec) {
        StressTaskRequest req = new StressTaskRequest();
        req.setTargetUrl(url);
        req.setConcurrency(concurrency);
        req.setDurationSec(durationSec);
        return req;
    }

    public String getTaskName() {
        return taskName;
    }

    public void setTaskName(String taskName) {
        this.taskName = taskName;
    }

    public String getEngineType() {
        return engineType;
    }

    public void setEngineType(String engineType) {
        this.engineType = engineType;
    }

    public String getTargetUrl() {
        return targetUrl;
    }

    public void setTargetUrl(String targetUrl) {
        this.targetUrl = targetUrl;
    }

    public String getAppName() {
        return appName;
    }

    public void setAppName(String appName) {
        this.appName = appName;
    }

    public String getMethod() {
        return method;
    }

    public void setMethod(String method) {
        this.method = method;
    }

    public int getConcurrency() {
        return concurrency;
    }

    public void setConcurrency(int concurrency) {
        this.concurrency = concurrency;
    }

    public int getDurationSec() {
        return durationSec;
    }

    public void setDurationSec(int durationSec) {
        this.durationSec = durationSec;
    }

    public int getQpsLimit() {
        return qpsLimit;
    }

    public void setQpsLimit(int qpsLimit) {
        this.qpsLimit = qpsLimit;
    }

    public int getWarmupSec() {
        return warmupSec;
    }

    public void setWarmupSec(int warmupSec) {
        this.warmupSec = warmupSec;
    }

    public int getRampUpSec() {
        return rampUpSec;
    }

    public void setRampUpSec(int rampUpSec) {
        this.rampUpSec = rampUpSec;
    }

    public Map<String, String> getHeaders() {
        return headers;
    }

    public void setHeaders(Map<String, String> headers) {
        this.headers = headers;
    }

    public String getBody() {
        return body;
    }

    public void setBody(String body) {
        this.body = body;
    }

    public List<Stage> getStages() {
        return stages;
    }

    public void setStages(List<Stage> stages) {
        this.stages = stages;
    }

    public int getSampleIntervalSec() {
        return sampleIntervalSec;
    }

    public void setSampleIntervalSec(int sampleIntervalSec) {
        this.sampleIntervalSec = sampleIntervalSec;
    }
}
