package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.deploy.DeployService;
import com.perfserver.entity.DeployConn;
import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.SourceRepoMapper;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

/**
 * 远程部署 API：SSH 连接配置管理 + 编译/部署启动/状态检查（AI 自动闭环亦可调用同一服务）。
 */
@RestController
@RequestMapping("/api/deploy")
public class DeployController {

    private final DeployService deployService;
    private final SourceRepoMapper sourceRepoMapper;

    public DeployController(DeployService deployService, SourceRepoMapper sourceRepoMapper) {
        this.deployService = deployService;
        this.sourceRepoMapper = sourceRepoMapper;
    }

    /** SSH 连接列表 */
    @GetMapping("/conns")
    public ApiResponse<List<DeployConn>> conns(@RequestParam(defaultValue = "50") int limit) {
        return ApiResponse.ok(deployService.listConns(limit));
    }

    /** 新增 SSH 连接（密码/私钥二选一） */
    @PostMapping("/conns")
    public ApiResponse<DeployConn> addConn(@RequestBody DeployConn conn) {
        if (conn.getConnName() == null || conn.getHost() == null || conn.getUsername() == null) {
            throw new IllegalArgumentException("connName/host/username 必填");
        }
        deployService.addConn(conn);
        return ApiResponse.ok(conn);
    }

    /** 删除 SSH 连接 */
    @DeleteMapping("/conns/{id}")
    public ApiResponse<Void> delConn(@PathVariable long id) {
        deployService.deleteConn(id);
        return ApiResponse.ok(null);
    }

    /** 平台侧编译被测项目（Maven；产物路径供 /deploy 使用） */
    @PostMapping("/build")
    public ApiResponse<Map<String, Object>> build(@RequestBody Map<String, Object> body) throws Exception {
        long repoId = Long.parseLong(String.valueOf(body.get("repoId")));
        SourceRepo repo = sourceRepoMapper.findById(repoId);
        if (repo == null || repo.getSourcePath() == null) {
            throw new IllegalArgumentException("源码仓库不存在或未落盘");
        }
        String module = body.get("module") == null ? null : String.valueOf(body.get("module"));
        DeployService.BuildResult r = deployService.build(repoId, module, repo.getSourcePath());
        return ApiResponse.ok(Map.of(
                "success", r.success(),
                "jarPath", r.jarPath() == null ? "" : r.jarPath().toString(),
                "logTail", r.logTail()));
    }

    /** 部署并启动（上传编译包 + 可选 agent，远程 nohup 启动并健康检查） */
    @PostMapping("/start")
    public ApiResponse<Map<String, Object>> start(@RequestBody Map<String, Object> body) throws Exception {
        long connId = Long.parseLong(String.valueOf(body.get("connId")));
        String jarPath = String.valueOf(body.get("jarPath"));
        String agentPath = body.get("agentPath") == null ? null : String.valueOf(body.get("agentPath"));
        int appPort = body.get("appPort") == null ? 8081 : Integer.parseInt(String.valueOf(body.get("appPort")));
        DeployService.DeployResult r = deployService.deployAndStart(
                connId, java.nio.file.Path.of(jarPath),
                agentPath == null || agentPath.isBlank() ? null : java.nio.file.Path.of(agentPath),
                "", appPort);
        return ApiResponse.ok(Map.of(
                "success", r.success(),
                "jar", r.jarName(),
                "agent", r.agentName() == null ? "" : r.agentName(),
                "pid", r.pid(),
                "healthHttpCode", r.healthCode(),
                "processStatus", r.processStatus()));
    }

    /** 远程应用状态（进程/健康检查/日志尾部） */
    @GetMapping("/status")
    public ApiResponse<String> status(@RequestParam long connId,
                                      @RequestParam(defaultValue = "8081") int appPort) throws Exception {
        return ApiResponse.ok(deployService.appStatus(connId, appPort));
    }
}

