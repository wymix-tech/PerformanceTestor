package com.perfserver.controller;

import com.perfserver.collector.AgentReportService;
import com.perfserver.common.ApiResponse;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * Agent 上报接口：被测服务器 agent 批量上报 trace / jvm / slowSql 数据。
 *
 * <p>接口设计为容错优先：即使单批数据解析失败也返回成功，避免 agent 端
 * 因平台返回错误而进入重试风暴，保证上报通道稳定。
 */
@RestController
@RequestMapping("/api/agent")
public class AgentReportController {

    private static final Logger log = LoggerFactory.getLogger(AgentReportController.class);

    private final AgentReportService agentReportService;

    public AgentReportController(AgentReportService agentReportService) {
        this.agentReportService = agentReportService;
    }

    /**
     * 接收 agent 批量上报（body：{"type","appName","targetProjectName","items"}）。
     * 项目不存在时返回 code=404 + message 含 PROJECT_NOT_FOUND（agent 识别后自动停报）。
     */
    @PostMapping("/report")
    public ApiResponse<Void> report(@RequestBody Map<String, Object> body, HttpServletRequest request) {
        String remoteHost = request.getRemoteAddr();
        String project = String.valueOf(body.getOrDefault("targetProjectName",
                body.getOrDefault("appName", "")));
        if (!agentReportService.projectExists(project)) {
            log.warn("agent report DROPPED: 项目「{}」不存在", project);
            return ApiResponse.error(404,
                    "PROJECT_NOT_FOUND: 项目「" + project + "」不存在于平台，数据已全部丢弃");
        }
        agentReportService.handleReport(body, remoteHost);
        return ApiResponse.ok();
    }
}
