package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.deploy.DeployService;
import org.springframework.stereotype.Component;

/**
 * remote_app_status AI 工具：检查被测服务器应用运行状态（进程/健康检查/日志尾部）。
 */
@Component
public class RemoteAppStatusTool extends AbstractAiTool {

    private final DeployService deployService;

    public RemoteAppStatusTool(DeployService deployService) {
        this.deployService = deployService;
    }

    @Override
    public String name() {
        return "remote_app_status";
    }

    @Override
    public String description() {
        return "检查被测服务器上应用运行状态（进程/HTTP 健康检查/日志尾部）。参数：connId SSH 连接配置 ID（必填）；"
                + "appPort 应用端口（默认 8081）。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"connId", "appPort"}, new String[]{"connId"}).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        var args = parse(jsonArgs);
        if (!args.hasNonNull("connId")) {
            return error("connId 必填");
        }
        try {
            int port = args.hasNonNull("appPort") ? args.get("appPort").asInt(8081) : 8081;
            String status = deployService.appStatus(args.get("connId").asLong(), port);
            ObjectNode out = mapper.createObjectNode();
            out.put("status", status);
            return ok(out);
        } catch (Exception e) {
            return error("状态检查失败: " + e.getMessage());
        }
    }
}

