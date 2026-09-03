package com.perfserver.entity;

import java.time.LocalDateTime;

/**
 * 接口驱动数据方案：按压测接口分析源码推导出的数据准备方案，对应 data_gen_plan 表。
 *
 * <p>落库后可回看（推荐理由带 {@code 文件:行号} 证据）、可重复执行。
 * 关键约束：{@code tablesJson} 中 seq 规则<b>只存 seqPrefix</b>，
 * {@code seqStart} 由执行时探测目标列 MAX(col)+1 刷新——固化起始值会让重复执行撞唯一键。</p>
 */
public class DataGenPlan {

    /** 分析中 / 分析完成待采纳（分析为异步，二者共用 pending） */
    public static final String ST_PENDING = "pending";
    public static final String ST_ADOPTED = "adopted";
    public static final String ST_EXECUTED = "executed";
    public static final String ST_DISCARDED = "discarded";
    /**
     * 分析失败。
     *
     * <p>必须显式区分：异步分析抛异常时若仍停留 {@link #ST_PENDING}，前端只能靠
     * 「表清单为空且无告警」猜测状态，与「分析正在进行中」无法区分——用户会一直等到轮询超时。</p>
     */
    public static final String ST_FAILED = "failed";

    private Long id;
    private Long repoId;
    private String planName;
    /** 接口/场景步骤快照 [{httpMethod, path, weight}] */
    private String endpointJson;
    /** 压测参数快照（concurrency/durationSec/qpsLimit/stages/scenario） */
    private String stressJson;
    /** 数据库连接配置 ID（与 dbUrl 二选一，服务层强校验） */
    private Long connId;
    private String dbUrl;
    /** 表清单 [{table, rowCount, fields, access, evidence}] */
    private String tablesJson;
    /** 降级与告警 JSON 数组 */
    private String warningsJson;
    private String status;
    private Long taskId;
    private LocalDateTime createTime;
    private LocalDateTime updateTime;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public Long getRepoId() { return repoId; }
    public void setRepoId(Long repoId) { this.repoId = repoId; }
    public String getPlanName() { return planName; }
    public void setPlanName(String planName) { this.planName = planName; }
    public String getEndpointJson() { return endpointJson; }
    public void setEndpointJson(String endpointJson) { this.endpointJson = endpointJson; }
    public String getStressJson() { return stressJson; }
    public void setStressJson(String stressJson) { this.stressJson = stressJson; }
    public Long getConnId() { return connId; }
    public void setConnId(Long connId) { this.connId = connId; }
    public String getDbUrl() { return dbUrl; }
    public void setDbUrl(String dbUrl) { this.dbUrl = dbUrl; }
    public String getTablesJson() { return tablesJson; }
    public void setTablesJson(String tablesJson) { this.tablesJson = tablesJson; }
    public String getWarningsJson() { return warningsJson; }
    public void setWarningsJson(String warningsJson) { this.warningsJson = warningsJson; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
    public Long getTaskId() { return taskId; }
    public void setTaskId(Long taskId) { this.taskId = taskId; }
    public LocalDateTime getCreateTime() { return createTime; }
    public void setCreateTime(LocalDateTime createTime) { this.createTime = createTime; }
    public LocalDateTime getUpdateTime() { return updateTime; }
    public void setUpdateTime(LocalDateTime updateTime) { this.updateTime = updateTime; }
}
