package com.perfserver.stress;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 压测任务请求：单档并发、分档阶梯（stages）、多接口场景（scenario）三种模式。
 *
 * <p>stages 模式用于容量评估：按多档并发依次执行，每档结束后生成一个采样点
 * （并发/QPS/RT 分位数），供容量曲线插值分析。
 *
 * <p>scenario 模式用于混合场景压测：按权重编排多个接口步骤，并支持从响应中
 * 提取登录态等变量在步骤间传递，更贴近真实流量模型。
 * 未配置 scenario 时退化为单接口模式，存量调用与前端旧表单零改动。
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
    /**
     * 结果判断（断言）规则：全部命中才算本次请求成功。
     *
     * <p>未配置时退化为历史行为——仅按 HTTP 状态码 >= 400 判定失败。
     * 断言失败的样本计入错误率，但不中断压测（统计口径与网络/状态码错误一致）。
     */
    private List<Assertion> assertions;
    /** 请求体（POST 等） */
    private String body;
    /** 分档并发模式（容量评估）：每档 {concurrency, durationSec} */
    private List<Stage> stages;
    /**
     * 分档并发的简写形式（前端表单用）：只给并发数列表，每档时长按
     * {@code durationSec / 档数} 均分，由 {@link #resolveStages()} 展开为 {@link #stages}。
     *
     * <p>存在意义：前端表单填「10,50,100」即可，不必构造完整 Stage 对象数组。
     */
    private List<Integer> stageConcurrencies;
    /** 采样周期秒（默认 5，用于实时图表） */
    private int sampleIntervalSec = 5;
    /**
     * 多接口混合场景步骤（为空 = 单接口模式，沿用 targetUrl/method/headers/body）。
     *
     * <p>每步带权重：按权重比例随机选中执行，模拟真实流量中不同接口的访问配比。
     */
    private List<ScenarioStep> scenario;
    /**
     * 场景初始变量（如预置 token），可被步骤提取结果覆盖。
     * 步骤 URL / 头 / body 中的 {@code {{var}}} 占位会被替换为变量值。
     */
    private Map<String, String> variables = new ConcurrentHashMap<>();

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

    /** 场景步骤：一个接口的一种调用形态 */
    public static class ScenarioStep {
        /** 步骤名（用于分步骤统计；留空按序号生成） */
        private String name;
        /** 请求地址，支持 {{var}} 占位 */
        private String url;
        private String method = "GET";
        private Map<String, String> headers = new HashMap<>();
        private String body;
        /** 权重配比（默认 1；各步权重之比即流量配比） */
        private int weight = 1;
        /** 从响应提取变量（如 data.token → token，或响应头 Set-Cookie），供后续步骤使用 */
        private List<VarExtract> extract;
        /** 结果判断（断言）规则：全部命中才算本步骤成功（未配置则仅按状态码判定） */
        private List<Assertion> assertions;

        public List<Assertion> getAssertions() {
            return assertions;
        }

        public void setAssertions(List<Assertion> assertions) {
            this.assertions = assertions;
        }

        public String getName() {
            return name;
        }

        public void setName(String name) {
            this.name = name;
        }

        public String getUrl() {
            return url;
        }

        public void setUrl(String url) {
            this.url = url;
        }

        public String getMethod() {
            return method;
        }

        public void setMethod(String method) {
            this.method = method;
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

        public int getWeight() {
            return weight;
        }

        public void setWeight(int weight) {
            this.weight = weight;
        }

        public List<VarExtract> getExtract() {
            return extract;
        }

        public void setExtract(List<VarExtract> extract) {
            this.extract = extract;
        }
    }

    /** 变量提取规则：从响应体 JSON 或响应头中取值 */
    public static class VarExtract {
        /** 变量名（后续步骤用 {{var}} 引用） */
        private String var;
        /**
         * 提取来源：body（默认，按点分 JSON 路径） / header（按响应头名）。
         *
         * <p>登录态常见的两种下发方式——JSON 体的 {@code data.token} 与响应头的
         * {@code Set-Cookie} / {@code X-Token}——故两者都需要支持。
         */
        private String source = "body";
        /** 点分路径（source=body）或响应头名（source=header） */
        private String path;

        public String getVar() {
            return var;
        }

        public void setVar(String var) {
            this.var = var;
        }

        public String getSource() {
            return source;
        }

        public void setSource(String source) {
            this.source = source;
        }

        public String getPath() {
            return path;
        }

        public void setPath(String path) {
            this.path = path;
        }
    }

    /**
     * 结果判断（断言）规则：对单次响应做校验，全部断言命中才算本次请求成功。
     *
     * <p>为什么需要：仅凭 HTTP 状态码无法识别「业务失败」——例如接口返回 200
     * 但 body 是 {@code {"code":500,"message":"库存不足"}}，或网关把后端错误
     * 包装成 200。这类响应在压测里必须计为错误，否则错误率严重失真。
     */
    public static class Assertion {
        /**
         * 断言来源：
         * <ul>
         *   <li>{@code status} — HTTP 状态码（key 忽略）</li>
         *   <li>{@code body}   — 响应体原文（key 忽略）</li>
         *   <li>{@code header} — 指定响应头（key = 头名，大小写不敏感）</li>
         *   <li>{@code json}   — 响应体 JSON 点分路径（key = 路径，如 data.code）</li>
         * </ul>
         */
        private String source = "status";
        /** 头名或 JSON 路径（status / body 断言忽略） */
        private String key;
        /**
         * 比较方式：{@code eq} / {@code ne} / {@code contains} / {@code notContains}
         * / {@code gt} / {@code lt} / {@code regex}
         */
        private String op = "eq";
        /** 期望值（gt / lt 按数值比较；status 配 gt/lt 可判定状态码区间） */
        private String expected;

        public String getSource() {
            return source;
        }

        public void setSource(String source) {
            this.source = source;
        }

        public String getKey() {
            return key;
        }

        public void setKey(String key) {
            this.key = key;
        }

        public String getOp() {
            return op;
        }

        public void setOp(String op) {
            this.op = op;
        }

        public String getExpected() {
            return expected;
        }

        public void setExpected(String expected) {
            this.expected = expected;
        }
    }

    /** 是否多接口场景模式（scenario 非空且含有效步骤） */
    public boolean isScenarioMode() {
        return scenario != null && !scenario.isEmpty();
    }

    /**
     * 取得生效的分档配置：优先 {@code stages}；为空时把 {@code stageConcurrencies}
     * 简写形式展开为完整分档（每档时长 = durationSec / 档数，至少 1 秒）。
     *
     * <p>做这一层归一化的原因：前端表单提交的是并发数简写列表，而引擎只认完整 Stage。
     * 若不做兼容，用户填了分档并发却会静默退化成单档压测，容量曲线无从产生。
     *
     * @return 生效的分档列表；无分档返回 null
     */
    public List<Stage> resolveStages() {
        if (stages != null && !stages.isEmpty()) {
            return stages;
        }
        if (stageConcurrencies == null || stageConcurrencies.isEmpty()) {
            return null;
        }
        List<Integer> valid = new ArrayList<>();
        for (Integer c : stageConcurrencies) {
            if (c != null && c > 0) {
                valid.add(c);
            }
        }
        if (valid.isEmpty()) {
            return null;
        }
        // 时长按档数均分，保证总耗时与 durationSec 大致相当（不再叠加 N 倍时长）
        int per = Math.max(1, durationSec / valid.size());
        List<Stage> resolved = new ArrayList<>(valid.size());
        for (Integer c : valid) {
            Stage s = new Stage();
            s.setConcurrency(c);
            s.setDurationSec(per);
            resolved.add(s);
        }
        return resolved;
    }

    public List<Integer> getStageConcurrencies() {
        return stageConcurrencies;
    }

    public void setStageConcurrencies(List<Integer> stageConcurrencies) {
        this.stageConcurrencies = stageConcurrencies;
    }

    public List<ScenarioStep> getScenario() {
        return scenario;
    }

    public void setScenario(List<ScenarioStep> scenario) {
        this.scenario = scenario;
    }

    public Map<String, String> getVariables() {
        return variables;
    }

    public void setVariables(Map<String, String> variables) {
        this.variables = variables == null ? new ConcurrentHashMap<>() : new ConcurrentHashMap<>(variables);
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

    public List<Assertion> getAssertions() {
        return assertions;
    }

    public void setAssertions(List<Assertion> assertions) {
        this.assertions = assertions;
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
