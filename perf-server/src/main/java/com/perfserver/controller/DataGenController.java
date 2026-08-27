package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.datagen.ColumnInfo;
import com.perfserver.datagen.DataGenRequest;
import com.perfserver.datagen.DataGenService;
import com.perfserver.entity.DataGenTask;
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
 */
@RestController
@RequestMapping("/api/datagen")
public class DataGenController {

    private final DataGenService dataGenService;

    public DataGenController(DataGenService dataGenService) {
        this.dataGenService = dataGenService;
    }

    /** 创建并启动灌数据任务（异步执行） */
    @PostMapping("/tasks")
    public ApiResponse<DataGenTask> start(@RequestBody DataGenRequest request) {
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

    /** 目标库表清单 */
    @PostMapping("/tables")
    public ApiResponse<List<String>> tables(@RequestBody Map<String, String> body) {
        return ApiResponse.ok(dataGenService.listTables(
                body.get("dbUrl"), body.get("dbUser"), body.get("dbPassword")));
    }

    /** 目标表列清单 */
    @PostMapping("/columns")
    public ApiResponse<List<ColumnInfo>> columns(@RequestBody Map<String, String> body) {
        return ApiResponse.ok(dataGenService.listColumns(
                body.get("dbUrl"), body.get("dbUser"), body.get("dbPassword"), body.get("tableName")));
    }
}
