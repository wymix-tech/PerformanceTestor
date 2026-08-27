package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.perfserver.datagen.DataGenRequest;
import com.perfserver.datagen.DataGenService;
import com.perfserver.entity.DataGenTask;
import org.springframework.stereotype.Component;

import java.util.Map;

/**
 * 自动灌数据工具：按目标表结构与行数批量生成测试数据（异步执行）。
 */
@Component
public class DataGenTool extends AbstractAiTool {

    private final DataGenService dataGenService;

    public DataGenTool(DataGenService dataGenService) {
        this.dataGenService = dataGenService;
    }

    @Override
    public String name() {
        return "data_gen";
    }

    @Override
    public String description() {
        return "向被测数据库自动灌入测试数据（压测前置准备）。参数：dbUrl 必填（jdbc:mysql:// 开头，含库名）；"
                + "tableName 目标表名；rowCount 生成行数（1-1000000，默认 1000）；taskName 任务名（可选）。"
                + "按表结构自动生成符合字段类型的数据并批量插入，返回任务 ID 与状态。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"dbUrl", "tableName", "rowCount", "taskName"},
                new String[]{"dbUrl", "tableName"}).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        String dbUrl = str(args, "dbUrl", "");
        String table = str(args, "tableName", "");
        if (dbUrl.isBlank() || table.isBlank()) {
            return error("dbUrl 与 tableName 必填");
        }
        try {
            DataGenRequest req = new DataGenRequest();
            req.setDbUrl(dbUrl);
            req.setTableName(table);
            req.setRowCount(integer(args, "rowCount", 1000));
            req.setTaskName(str(args, "taskName", null));
            DataGenTask task = dataGenService.createAndRun(req);
            return ok(Map.of("taskId", task.getId(), "status", task.getStatus(),
                    "tableName", table, "rowCount", req.getRowCount()));
        } catch (Exception e) {
            return error("灌数据任务启动失败: " + e.getMessage());
        }
    }
}
