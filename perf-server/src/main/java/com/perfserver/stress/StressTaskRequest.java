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
    /** HTTP 方法（GET/POST/...） */
    private String method = "GET";
    /** 并发数（单档模式） */
    private int concurrency = 1;
    /** 持续时长秒（单档模式） */
    private int durationSec = 60;
    /** QPS 上限（0 不限制） */
    private int qpsLimit;
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
