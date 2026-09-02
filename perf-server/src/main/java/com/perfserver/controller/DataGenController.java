package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.datagen.ColumnInfo;
import com.perfserver.datagen.DataGenRequest;
import com.perfserver.datagen.DataGenService;
import com.perfserver.datagen.plan.EndpointDataPlanService;
import com.perfserver.entity.DataGenPlan;
import com.perfserver.entity.DataGenTask;
import com.perfserver.entity.DbConn;
import com.perfserver.entity.SemanticHint;
import com.perfserver.mapper.DbConnMapper;
import com.perfserver.security.CryptoService;
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
 * 数据准备 API：灌数据任务启动/查询（含 Dry-Run 试运行）、目标库表/列结构探查（含枚举建议）、
 * 语义建议查询与采纳（魔法值/表间关联）。
 */
@RestController
@RequestMapping("/api/datagen")
public class DataGenController {

    private final DataGenService dataGenService;
    private final DbConnMapper dbConnMapper;
    private final CryptoService cryptoService;
    private final EndpointDataPlanService planService;

    public DataGenController(DataGenService dataGenService, DbConnMapper dbConnMapper,
                             CryptoService cryptoService, EndpointDataPlanService planService) {
        this.dataGenService = dataGenService;
        this.dbConnMapper = dbConnMapper;
        this.cryptoService = cryptoService;
        this.planService = planService;
    }

    /** 按 connId 取连接凭据并覆盖手填值（connId 为空、非数字或不存在时原样返回） */
    private void applyConn(Map<String, String> body) {
        String connIdStr = body.get("connId");
        if (connIdStr == null || connIdStr.isBlank()) {
            return;
        }
        DbConn conn;
        try {
            conn = dbConnMapper.findById(Long.parseLong(connIdStr.trim()));
        } catch (NumberFormatException e) {
            // connId 非数字：视同未指定，继续用手填凭据，不因辅助参数报错
            return;
        }
        if (conn != null) {
            body.put("dbUrl", conn.getDbUrl());
            body.put("dbUser", conn.getUsername());
            body.put("dbPassword", cryptoService.decrypt(conn.getPassword()));
        }
    }

    /** 创建并启动灌数据任务（异步执行；connId 提供时复用连接配置凭据） */
    @PostMapping("/tasks")
    public ApiResponse<DataGenTask> start(@RequestBody DataGenRequest request) {
        if (request.getConnId() != null) {
            DbConn conn = dbConnMapper.findById(request.getConnId());
            if (conn != null) {
                request.setDbUrl(conn.getDbUrl());
                request.setDbUser(conn.getUsername());
                request.setDbPassword(cryptoService.decrypt(conn.getPassword()));
            }
        }
        return ApiResponse.ok(dataGenService.createAndRun(request));
    }

    /** 任务列表 */
    @GetMapping("/tasks")
    public ApiResponse<List<DataGenTask>> tasks(@RequestParam(defaultValue = "50") int limit) {
        return ApiResponse.ok(dataGenService.list(limit));
    }

    /** 任务详情 */
    @GetMapping("/tasks/{id}")
    public ApiResponse<DataGenTask> detail(@PathVariable long id) {
        return ApiResponse.ok(dataGenService.detail(id));
    }

    /** 目标库表清单（connId 提供时复用连接配置凭据） */
    @PostMapping("/tables")
    public ApiResponse<List<String>> tables(@RequestBody Map<String, String> body) {
        applyConn(body);
        return ApiResponse.ok(dataGenService.listTables(
                body.get("dbUrl"), body.get("dbUser"), body.get("dbPassword")));
    }

    /** 目标表列清单（含枚举建议值 suggestedValues 与疑似枚举标记 suspectEnum） */
    @PostMapping("/columns")
    public ApiResponse<List<ColumnInfo>> columns(@RequestBody Map<String, String> body) {
        applyConn(body);
        return ApiResponse.ok(dataGenService.listColumns(
                body.get("dbUrl"), body.get("dbUser"), body.get("dbPassword"), body.get("tableName")));
    }

    /** 语义建议查询（hintType=value/relation；status=pending 查看待确认项） */
    @GetMapping("/suggestions")
    public ApiResponse<List<SemanticHint>> suggestions(
            @RequestParam(required = false) Long repoId,
            @RequestParam(required = false) String tableName,
            @RequestParam(required = false) String hintType,
            @RequestParam(required = false) String status) {
        return ApiResponse.ok(dataGenService.suggestions(repoId, tableName, hintType, status));
    }

    /** 建议采纳/忽略（adopt=true 采纳为生成规则依据） */
    @PostMapping("/suggestions/{id}/adopt")
    public ApiResponse<Void> adopt(@PathVariable long id,
                                   @RequestParam(defaultValue = "true") boolean adopt) {
        dataGenService.adopt(id, adopt);
        return ApiResponse.ok(null);
    }

    // ===== 接口驱动数据方案 =====

    /** 按接口分析源码并生成数据方案（异步分析，返回已落库的方案） */
    @PostMapping("/plans/analyze")
    public ApiResponse<DataGenPlan> analyzePlan(
            @RequestBody com.perfserver.datagen.plan.EndpointDataPlanService.AnalyzeRequest request) {
        return ApiResponse.ok(planService.analyze(request));
    }

    /** 方案列表（按仓库 + 时间倒序） */
    @GetMapping("/plans")
    public ApiResponse<List<DataGenPlan>> plans(
            @RequestParam(required = false) Long repoId,
            @RequestParam(required = false) String status,
            @RequestParam(defaultValue = "50") int limit) {
        return ApiResponse.ok(planService.list(repoId, status, limit));
    }

    /** 方案详情（表清单 / 规则 / 证据 / 告警） */
    @GetMapping("/plans/{id}")
    public ApiResponse<DataGenPlan> planDetail(@PathVariable long id) {
        return ApiResponse.ok(planService.detail(id));
    }

    /** 方案采纳/放弃 */
    @PostMapping("/plans/{id}/adopt")
    public ApiResponse<DataGenPlan> adoptPlan(@PathVariable long id,
                                              @RequestParam(defaultValue = "true") boolean adopt) {
        return ApiResponse.ok(planService.adopt(id, adopt));
    }

    /**
     * 按方案执行灌数（dryRun=true 时仅试运行）。
     *
     * <p>请求体可选带 {@code connId} 或 {@code dbUrl}：方案在分析阶段允许不指定连接
     * （分析只读源码），执行时必须补上，故支持执行时由前端按当前选定值传入。</p>
     */
    @PostMapping("/plans/{id}/execute")
    public ApiResponse<DataGenTask> executePlan(@PathVariable long id,
                                                @RequestParam(defaultValue = "true") boolean dryRun,
                                                @RequestBody(required = false) Map<String, Object> conn) {
        Map<String, Integer> rowOverrides = parseRowOverrides(conn);
        if (conn != null && !conn.isEmpty() && dataGenService.planNeedsConnection(id)) {
            dataGenService.applyPlanConnection(id, conn);
        }
        return ApiResponse.ok(dataGenService.runFromPlan(id, dryRun, rowOverrides));
    }

    /** 解析用户在方案卡上改过的行数：{rowOverrides:[{table,rowCount}]} */
    private static Map<String, Integer> parseRowOverrides(Map<String, Object> body) {
        Map<String, Integer> out = new java.util.LinkedHashMap<>();
        if (body == null || body.get("rowOverrides") == null) {
            return out;
        }
        Object raw = body.get("rowOverrides");
        if (!(raw instanceof java.util.List<?> list)) {
            return out;
        }
        for (Object item : list) {
            if (!(item instanceof Map<?, ?> m)) {
                continue;
            }
            Object table = m.get("table");
            Object rows = m.get("rowCount");
            if (table == null || rows == null) {
                continue;
            }
            String name = String.valueOf(table).trim().toLowerCase(java.util.Locale.ROOT);
            try {
                int v = rows instanceof Number n ? n.intValue() : Integer.parseInt(String.valueOf(rows).trim());
                if (!name.isEmpty() && v > 0) {
                    out.put(name, v);
                }
            } catch (NumberFormatException ignore) {
                // 非法值跳过，按方案落库值执行
            }
        }
        return out;
    }

    /** 删除方案 */
    @DeleteMapping("/plans/{id}")
    public ApiResponse<Void> deletePlan(@PathVariable long id) {
        planService.delete(id);
        return ApiResponse.ok(null);
    }
}
