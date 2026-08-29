package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 被测服务器 SSH 连接配置（deploy_conn 表）：AI 编排远程部署的凭据与工作目录。
 * 凭据仅服务端使用，不出现在 AI 上下文（AI 只引用 connId）。
 */
public class DeployConn {

    public static final String AUTH_PASSWORD = "password";
    public static final String AUTH_PRIVATE_KEY = "privateKey";

    private Long id;
    private String connName;
    private String host;
    private Integer port;
    private String username;
    private String authType;
    private String password;
    private String privateKey;
    /** 远程工作目录（上传编译包/agent 与执行启动命令） */
    private String workDir;
    private String remark;
    private LocalDateTime createTime;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public String getConnName() { return connName; }
    public void setConnName(String connName) { this.connName = connName; }
    public String getHost() { return host; }
    public void setHost(String host) { this.host = host; }
    public Integer getPort() { return port; }
    public void setPort(Integer port) { this.port = port; }
    public String getUsername() { return username; }
    public void setUsername(String username) { this.username = username; }
    public String getAuthType() { return authType; }
    public void setAuthType(String authType) { this.authType = authType; }
    public String getPassword() { return password; }
    public void setPassword(String password) { this.password = password; }
    public String getPrivateKey() { return privateKey; }
    public void setPrivateKey(String privateKey) { this.privateKey = privateKey; }
    public String getWorkDir() { return workDir; }
    public void setWorkDir(String workDir) { this.workDir = workDir; }
    public String getRemark() { return remark; }
    public void setRemark(String remark) { this.remark = remark; }
    public LocalDateTime getCreateTime() { return createTime; }
    public void setCreateTime(LocalDateTime createTime) { this.createTime = createTime; }
}

