package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.deploy.DeployService;
import org.springframework.stereotype.Component;

import java.nio.file.Files;
import java.nio.file.Path;

/**
 * deploy_and_start AI 工具：SFTP 上传编译包（与 agent）到被测服务器并远程启动。
 * 编译包路径来自 build_project 返回；agent 可选（非空则 -javaagent 挂载）。
 */
@Component
public class DeployAndStartTool extends AbstractAiTool {

    private final DeployService deployService;

    public DeployAndStartTool(DeployService deployService) {
        this.deployService = deployService;
    }

    @Override
    public String name() {
        return "deploy_and_start";
    }

    @Override
    public String description() {
        return "上传编译包到被测服务器（SSH）并启动应用。参数：connId SSH 连接配置 ID（必填，凭据服务端读取）；"
                + "jarPath 平台侧编译包绝对路径（来自 build_project 返回）；agentPath 平台侧 agent 包路径（可选，"
                + "默认 perf-agent/target/perf-agent.jar 自动探测）；appPort 应用端口（默认 8081，启动后健康检查）。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"connId", "jarPath", "agentPath", "appPort"}, new String[]{"connId", "jarPath"}).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        if (!args.hasNonNull("connId") || !args.hasNonNull("jarPath")) {
            return error("connId 与 jarPath 必填");
        }
        try {
            Path jar = Path.of(args.get("jarPath").asText());
            if (!Files.isRegularFile(jar)) {
                return error("编译包不存在: " + jar);
            }
            Path agent = null;
            if (args.hasNonNull("agentPath")) {
                agent = Path.of(args.get("agentPath").asText());
            } else {
                Path candidate = Path.of("perf-agent/target/perf-agent.jar");
                agent = Files.isRegularFile(candidate) ? candidate.toAbsolutePath() : null;
            }
            int port = args.hasNonNull("appPort") ? args.get("appPort").asInt(8081) : 8081;
            DeployService.DeployResult r = deployService.deployAndStart(
                    args.get("connId").asLong(), jar, agent, "", port);
            ObjectNode out = mapper.createObjectNode();
            out.put("success", r.success());
            out.put("jar", r.jarName());
            out.put("agent", r.agentName() == null ? "未挂载" : r.agentName());
            out.put("pid", r.pid());
            out.put("healthHttpCode", r.healthCode());
            out.put("processStatus", r.processStatus());
            out.put("note", "启动后建议立即用 stress_test 发起压测，并用 remote_app_status 观察运行状态");
            return ok(out);
        } catch (Exception e) {
            return error("部署失败: " + e.getMessage());
        }
    }
}

