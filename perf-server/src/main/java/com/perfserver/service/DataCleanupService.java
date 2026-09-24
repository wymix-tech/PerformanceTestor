package com.perfserver.service;

import com.perfserver.mapper.DataCleanupMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 项目历史数据清理：一键删除某应用（app_name）关联的全部采集与派生数据
 * （压测任务/采样、慢 SQL、慢命令、JVM 监控、调用链、性能问题/修复方案、应用实例、数据库连接、AI 结论）。
 * 项目本身（源码仓库与文件）不在清理范围。
 *
 * <p><b>全局 AI 结论（{@code ai_knowledge.app_name = ''}）默认保留</b>：该表存的是
 * "跨项目共性经验"，检索条件为 {@code app_name = #{appName} OR app_name = ''}，
 * 对所有应用可见；被某次按应用清理顺带删掉会静默拉低其他项目的分析质量，且无痕迹。
 * 需要连全局结论一起清时，必须由调用方显式传 {@code includeGlobalKnowledge=true}
 * 并完成二次确认（见 {@link #cleanupByAppName(String, boolean)}）。决策见方案文档 §二·5。
 */
@Service
public class DataCleanupService {

    private static final Logger log = LoggerFactory.getLogger(DataCleanupService.class);

    private final DataCleanupMapper cleanupMapper;

    public DataCleanupService(DataCleanupMapper cleanupMapper) {
        this.cleanupMapper = cleanupMapper;
    }

    /** 清理指定应用的全部历史数据（全局 AI 结论保留），返回各表删除行数 */
    @Transactional
    public Map<String, Integer> cleanupByAppName(String appName) {
        return cleanupByAppName(appName, false);
    }

    /**
     * 清理指定应用的全部历史数据。
     *
     * @param appName                应用名（必填；空则拒绝，防止无过滤删除）
     * @param includeGlobalKnowledge 是否连带删除 {@code ai_knowledge} 中 {@code app_name = ''} 的全局结论，
     *                               默认 {@code false}。置 {@code true} 属高风险操作，调用方须二次确认。
     * @return 各表删除行数；AI 结论拆两个键：{@code aiKnowledge}（本应用）与
     *         {@code aiKnowledgeGlobal}（全局，未开启时恒为 0），避免前端把两种删除显示成同一个数字
     */
    @Transactional
    public Map<String, Integer> cleanupByAppName(String appName, boolean includeGlobalKnowledge) {
        if (appName == null || appName.isBlank()) {
            throw new IllegalArgumentException("应用名（appName）不能为空，拒绝执行清理");
        }
        String app = appName.trim();
        Map<String, Integer> deleted = new LinkedHashMap<>();

        // 压测：先删采样（按任务 ID 级联），再删任务
        List<Long> taskIds = cleanupMapper.findStressTaskIdsByApp(app);
        deleted.put("stressSample", taskIds.isEmpty() ? 0 : cleanupMapper.deleteStressSamplesByTaskIds(taskIds));
        deleted.put("stressTask", cleanupMapper.deleteStressTasksByApp(app));

        // 问题：先删方案（按问题 ID 级联），再删问题
        List<Long> issueIds = cleanupMapper.findIssueIdsByApp(app);
        deleted.put("fixSuggestion", issueIds.isEmpty() ? 0 : cleanupMapper.deleteFixByIssueIds(issueIds));
        deleted.put("performanceIssue", cleanupMapper.deleteIssuesByApp(app));

        deleted.put("traceChain", cleanupMapper.deleteTraceByApp(app));
        deleted.put("jvmMetric", cleanupMapper.deleteJvmByApp(app));
        deleted.put("slowSql", cleanupMapper.deleteSlowSqlByApp(app));
        deleted.put("slowCommand", cleanupMapper.deleteSlowCommandByApp(app));
        deleted.put("appInstance", cleanupMapper.deleteAppInstancesByApp(app));
        deleted.put("dbConn", cleanupMapper.deleteDbConnsByApp(app));

        // AI 知识库：本应用结论（不含全局）；全局结论仅在显式开启时删除
        deleted.put("aiKnowledge", cleanupMapper.deleteAiKnowledgeByApp(app));
        int globalDeleted = 0;
        if (includeGlobalKnowledge) {
            globalDeleted = cleanupMapper.deleteGlobalAiKnowledge();
            // 审计日志：删全局结论影响所有项目，必须可追溯（操作人由 Controller 补充记录）
            log.warn("审计: 清理时连带删除全局 AI 结论, appName={}, aiKnowledgeGlobal={}", app, globalDeleted);
        }
        deleted.put("aiKnowledgeGlobal", globalDeleted);

        return deleted;
    }
}
