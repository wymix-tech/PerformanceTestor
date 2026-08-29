package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.deploy.DeployService;
import org.springframework.stereotype.Component;

/**
 * build_project AI 工具：平台侧编译被测项目（源码仓库已导入平台，沿用平台 Maven 镜像）。
 * 返回编译产物绝对路径，供 deploy_and_start 上传。
 */
@Component
public class BuildProjectTool extends AbstractAiTool {

    private final DeployService deployService;
    private final com.perfserver.mapper.SourceRepoMapper repoMapper;

    public BuildProjectTool(DeployService deployService, com.perfserver.mapper.SourceRepoMapper repoMapper) {
        this.deployService = deployService;
        this.repoMapper = repoMapper;
    }

    @Override
    public String name() {
        return "build_project";
    }

    @Override
    public String description() {
        return "平台侧编译被测项目（Maven）。参数：repoId 源码仓库 ID（必填）；module 可选 Maven 模块名。"
                + "返回编译结果与 jar 产物路径（供 deploy_and_start 上传启动）。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"repoId", "module"}, new String[]{"repoId"}).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        if (!args.hasNonNull("repoId")) {
            return error("repoId 必填");
        }
        try {
            var repo = repoMapper.findById(args.get("repoId").asLong());
            if (repo == null || repo.getSourcePath() == null) {
                return error("源码仓库不存在或未落盘（repoId=" + args.get("repoId").asLong() + "）");
            }
            DeployService.BuildResult r = deployService.build(repo.getId(), str(args, "module", null), repo.getSourcePath());
            ObjectNode out = mapper.createObjectNode();
            out.put("success", r.success());
            if (r.success()) {
                out.put("jarPath", r.jarPath().toString());
                out.put("jarName", r.jarPath().getFileName().toString());
                out.put("note", "编译成功，可调用 deploy_and_start 上传到目标服务器并启动");
            } else {
                out.put("logTail", r.logTail());
            }
            return ok(out);
        } catch (Exception e) {
            return error("编译失败: " + e.getMessage());
        }
    }
}

