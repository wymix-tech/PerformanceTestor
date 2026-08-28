package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.datagen.ColumnInfo;
import com.perfserver.datagen.DataGenRequest;
import com.perfserver.datagen.DataGenService;
import com.perfserver.entity.DataGenTask;
import com.perfserver.entity.DbConn;
import com.perfserver.mapper.DbConnMapper;
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
 * 数据准备 API：灌数据任务启动/查询、目标库表/列结构探查。
 * 连接凭据支持直接手填，也支持传 connId 复用"数据库连接配置"（与慢 SQL 分析共用）。
 */
@RestController
@RequestMapping("/api/datagen")
public class DataGenController {

    private final DataGenService dataGenService;
    private final DbConnMapper dbConnMapper;

    public DataGenController(DataGenService dataGenService, DbConnMapper dbConnMapper) {
        this.dataGenService = dataGenService;
        this.dbConnMapper = dbConnMapper;
    }

    /** 按 connId 取连接凭据并覆盖手填值（connId 为空或不存在时原样返回） */
    private void applyConn(Map<String, String> body) {
        String connIdStr = body.get("connId");
        if (connIdStr == null || connIdStr.isBlank()) {
            return;
        }
        DbConn conn = dbConnMapper.findById(Long.parseLong(connIdStr));
        if (conn != null) {
            body.put("dbUrl", conn.getDbUrl());
            body.put("dbUser", conn.getUsername());
            body.put("dbPassword", conn.getPassword() == null ? "" : conn.getPassword());
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
                request.setDbPassword(conn.getPassword() == null ? "" : conn.getPassword());
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

    /** 目标表列清单（connId 提供时复用连接配置凭据） */
    @PostMapping("/columns")
    public ApiResponse<List<ColumnInfo>> columns(@RequestBody Map<String, String> body) {
        applyConn(body);
        return ApiResponse.ok(dataGenService.listColumns(
                body.get("dbUrl"), body.get("dbUser"), body.get("dbPassword"), body.get("tableName")));
    }
}
