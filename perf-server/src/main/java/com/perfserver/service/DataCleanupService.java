package com.perfserver.service;

import com.perfserver.mapper.DataCleanupMapper;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 项目历史数据清理：一键删除某应用（app_name）关联的全部采集与派生数据
 * （压测任务/采样、慢 SQL、JVM 监控、调用链、性能问题/修复方案、应用实例、数据库连接）。
 * 项目本身（源码仓库与文件）不在清理范围。
 */
@Service
public class DataCleanupService {

    private final DataCleanupMapper cleanupMapper;

    public DataCleanupService(DataCleanupMapper cleanupMapper) {
        this.cleanupMapper = cleanupMapper;
    }

    /** 清理指定应用的全部历史数据，返回各表删除行数 */
    @Transactional
    public Map<String, Integer> cleanupByAppName(String appName) {
        Map<String, Integer> deleted = new LinkedHashMap<>();

        // 压测：先删采样（按任务 ID 级联），再删任务
        List<Long> taskIds = cleanupMapper.findStressTaskIdsByApp(appName);
        deleted.put("stressSample", taskIds.isEmpty() ? 0 : cleanupMapper.deleteStressSamplesByTaskIds(taskIds));
        deleted.put("stressTask", cleanupMapper.deleteStressTasksByApp(appName));

        // 问题：先删方案（按问题 ID 级联），再删问题
        List<Long> issueIds = cleanupMapper.findIssueIdsByApp(appName);
        deleted.put("fixSuggestion", issueIds.isEmpty() ? 0 : cleanupMapper.deleteFixByIssueIds(issueIds));
        deleted.put("performanceIssue", cleanupMapper.deleteIssuesByApp(appName));

        deleted.put("traceChain", cleanupMapper.deleteTraceByApp(appName));
        deleted.put("jvmMetric", cleanupMapper.deleteJvmByApp(appName));
        deleted.put("slowSql", cleanupMapper.deleteSlowSqlByApp(appName));
        deleted.put("appInstance", cleanupMapper.deleteAppInstancesByApp(appName));
        deleted.put("dbConn", cleanupMapper.deleteDbConnsByApp(appName));

        return deleted;
    }
}
