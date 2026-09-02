package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.entity.StressTask;
import com.perfserver.service.StressTaskService;
import com.perfserver.stress.StressTaskRequest;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 压测工具：发起 HTTP 压测任务（异步执行，立即返回任务 ID 与状态）。
 *
 * <p>支持两种形态：
 * <ul>
 *   <li><b>单接口</b>：给 {@code targetUrl}，对该地址持续发压；</li>
 *   <li><b>多接口混合场景</b>：给 {@code scenario} 步骤数组，按 weight 权重配比编排，
 *       并可用 {@code {{pN.result.path}}} / {@code {{pN.header.Name}}} /
 *       {@code {{alias.result.path}}} <b>直接引用上游步骤的响应</b>——无需声明 extract。</li>
 * </ul>
 */
@Component
public class StressTool extends AbstractAiTool {

    private final StressTaskService stressTaskService;

    public StressTool(StressTaskService stressTaskService) {
        this.stressTaskService = stressTaskService;
    }

    @Override
    public String name() {
        return "stress_test";
    }

    @Override
    public String description() {
        return "发起一次 HTTP 性能压测。二选一：① 单接口——targetUrl（http/https 开头）；"
                + "② 多接口混合场景——scenario 步骤数组（每项 url 必填，可选 name/alias/method/headers/body/weight/extract/assertions），"
                + "按 weight 权重配比发压。通用参数：concurrency 并发数（默认 20，1-1000）；"
                + "durationSec 持续秒数（默认 30，1-3600）；warmupSec 预热秒数（默认 10，0 关闭）；qpsLimit QPS 上限（0 不限）。"
                + "步骤间传值：下游步骤可在 url/请求头值/body 中直接写 {{p1.result.data.token}}（引用第 1 步响应体的 data.token）、"
                + "{{p1.header.X-Request-Id}}（引用第 1 步响应头）、{{login.result.userId}}（用 alias 指代步骤），无需 extract；"
                + "pN 为步骤序号（从 1 开始），也可用步骤的 alias 或 name。未命中时保留占位符原文，不影响压测继续。"
                + "返回任务 ID 与状态，任务异步执行，完成后可查询结果与容量评估。";
    }

    @Override
    public String parametersJson() {
        ObjectNode props = mapper.createObjectNode();
        props.set("targetUrl", prop("string", "单接口模式的压测目标地址（http/https 开头）；填 scenario 时可省略"));
        props.set("concurrency", prop("integer", "并发数，默认 20"));
        props.set("durationSec", prop("integer", "持续秒数，默认 30"));
        props.set("warmupSec", prop("integer", "预热秒数（不计入统计），默认 10，设 0 关闭"));
        props.set("qpsLimit", prop("integer", "QPS 上限，0 表示不限"));
        props.set("scenario", arrayProp(
                "多接口混合场景步骤数组；与 targetUrl 二选一。下游步骤可直接用 "
                        + "{{pN.result.path}} / {{pN.header.Name}} / {{alias.result.path}} 引用上游响应，无需 extract",
                scenarioItemSchema()));
        return buildParams(props, new String[]{}).toString();
    }

    /** scenario 数组元素的形状描述（引导 LLM 产出结构正确的步骤） */
    private ObjectNode scenarioItemSchema() {
        ObjectNode item = mapper.createObjectNode();
        item.put("type", "object");
        ObjectNode p = item.putObject("properties");
        p.set("url", prop("string", "步骤请求地址（必填）；可含 {{pN.result.x}} 等占位符"));
        p.set("alias", prop("string", "步骤别名，供 {{alias.result.x}} 引用；不填则可用 pN 或 name"));
        p.set("name", prop("string", "步骤名（分步骤统计维度）；不填时 alias 缺省回退用它"));
        p.set("method", prop("string", "HTTP 方法，默认 GET"));
        p.set("weight", prop("integer", "权重配比（各步权重之比即流量配比），默认 1"));
        p.set("headers", prop("object", "请求头，值为字符串；值支持占位符"));
        p.set("body", prop("string", "请求体；支持占位符"));
        p.set("extract", arrayProp("可选：从响应提取变量到 {{var}}（多数场景用上下文引用即可，无需声明）", null));
        p.set("assertions", arrayProp("可选：结果判断规则", null));
        var req = item.putArray("required");
        req.add("url");
        return item;
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        JsonNode scenarioArgs = arr(args, "scenario");
        String url = str(args, "targetUrl", "");
        if (url.isBlank() && scenarioArgs.isEmpty()) {
            return error("targetUrl 与 scenario 至少填一个（单接口填 targetUrl，多接口混合填 scenario 步骤数组）");
        }
        try {
            StressTaskRequest req = new StressTaskRequest();
            req.setTargetUrl(url);
            req.setConcurrency(integer(args, "concurrency", 20));
            req.setDurationSec(integer(args, "durationSec", 30));
            req.setWarmupSec(integer(args, "warmupSec", 10));
            req.setQpsLimit(integer(args, "qpsLimit", 0));
            req.setMethod(str(args, "method", "GET"));
            if (!scenarioArgs.isEmpty()) {
                List<StressTaskRequest.ScenarioStep> steps = new ArrayList<>();
                for (int i = 0; i < scenarioArgs.size(); i++) {
                    JsonNode n = scenarioArgs.get(i);
                    String stepUrl = str(n, "url", "");
                    if (stepUrl.isBlank()) {
                        return error("scenario 第 " + (i + 1) + " 步缺少 url（步骤内 url 必填）");
                    }
                    steps.add(toStep(n, stepUrl));
                }
                req.setScenario(steps);
                // 场景模式忽略 targetUrl/method，但报告页与历史趋势按 targetUrl 归集，
                // 未显式提供时退回首步地址，避免报告里出现空目标
                if (url.isBlank()) {
                    req.setTargetUrl(steps.get(0).getUrl());
                }
            }
            StressTask task = stressTaskService.createAndRun(req);
            Map<String, Object> data = new LinkedHashMap<>();
            data.put("taskId", task.getId());
            data.put("status", task.getStatus());
            data.put("concurrency", req.getConcurrency());
            data.put("durationSec", req.getDurationSec());
            if (req.isScenarioMode()) {
                data.put("scenarioSteps", req.getScenario().size());
                data.put("hint", "多接口场景任务异步执行中；下游步骤可直接用 "
                        + "{{pN.result.path}} / {{pN.header.Name}} / {{alias.result.path}} 引用上游响应，无需 extract。"
                        + "配置告警见结果 warnings，引用未命中次数见分步骤统计 renderMiss");
            } else {
                data.put("targetUrl", url);
                data.put("hint", "任务异步执行中，可用慢 SQL/容量工具或稍后查询结果");
            }
            return ok(data);
        } catch (Exception e) {
            return error("压测启动失败: " + e.getMessage());
        }
    }

    /** 把 AI 产出的步骤 JSON 装配成引擎侧步骤对象 */
    private StressTaskRequest.ScenarioStep toStep(JsonNode n, String url) {
        StressTaskRequest.ScenarioStep step = new StressTaskRequest.ScenarioStep();
        step.setUrl(url);
        step.setAlias(str(n, "alias", null));
        step.setName(str(n, "name", null));
        step.setMethod(str(n, "method", "GET"));
        step.setWeight(integer(n, "weight", 1));
        step.setBody(str(n, "body", null));
        JsonNode headers = n.path("headers");
        if (headers.isObject()) {
            Map<String, String> map = new LinkedHashMap<>();
            headers.fields().forEachRemaining(e -> {
                JsonNode v = e.getValue();
                map.put(e.getKey(), v.isValueNode() ? v.asText() : v.toString());
            });
            step.setHeaders(map);
        }
        JsonNode extract = arr(n, "extract");
        if (extract.size() > 0) {
            List<StressTaskRequest.VarExtract> rules = new ArrayList<>();
            extract.forEach(x -> {
                StressTaskRequest.VarExtract rule = new StressTaskRequest.VarExtract();
                rule.setVar(str(x, "var", null));
                rule.setSource(str(x, "source", "body"));
                rule.setPath(str(x, "path", null));
                if (rule.getVar() != null && rule.getPath() != null) {
                    rules.add(rule);
                }
            });
            step.setExtract(rules);
        }
        JsonNode assertions = arr(n, "assertions");
        if (assertions.size() > 0) {
            List<StressTaskRequest.Assertion> rules = new ArrayList<>();
            assertions.forEach(x -> {
                StressTaskRequest.Assertion rule = new StressTaskRequest.Assertion();
                rule.setSource(str(x, "source", "status"));
                rule.setKey(str(x, "key", null));
                rule.setOp(str(x, "op", "eq"));
                rule.setExpected(str(x, "expected", null));
                rules.add(rule);
            });
            step.setAssertions(rules);
        }
        return step;
    }
}
