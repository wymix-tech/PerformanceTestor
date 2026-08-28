package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.DbConn;
import com.perfserver.mapper.DbConnMapper;
import com.perfserver.service.DbExplainService;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

/**
 * 数据库连接配置：供慢 SQL 分析时执行 EXPLAIN 等只读检测。
 * 密码内网演示明文存储，生产环境应加密或接入凭据管理。
 */
@RestController
@RequestMapping("/api/dbconns")
public class DbConnController {

    private final DbConnMapper dbConnMapper;
    private final DbExplainService dbExplainService;

    public DbConnController(DbConnMapper dbConnMapper, DbExplainService dbExplainService) {
        this.dbConnMapper = dbConnMapper;
        this.dbExplainService = dbExplainService;
    }

    /** 连接列表 */
    @GetMapping
    public ApiResponse<List<DbConn>> list() {
        return ApiResponse.ok(dbConnMapper.list(50));
    }

    /** 保存连接（isDefault=1 时先清空其他默认） */
    @PostMapping
    public ApiResponse<DbConn> save(@RequestBody DbConn conn) {
        if (conn.getConnName() == null || conn.getConnName().isBlank()) {
            throw new IllegalArgumentException("连接名称不能为空");
        }
        if (conn.getDbUrl() == null || !conn.getDbUrl().trim().startsWith("jdbc:")) {
            throw new IllegalArgumentException("JDBC URL 必须以 jdbc: 开头");
        }
        if (conn.getUsername() == null || conn.getUsername().isBlank()) {
            throw new IllegalArgumentException("用户名不能为空");
        }
        // NOT NULL 列归一化，避免前端缺省字段导致插入失败
        if (conn.getAppName() == null) {
            conn.setAppName("");
        }
        if (conn.getHost() == null) {
            conn.setHost("");
        }
        if (conn.getPassword() == null) {
            conn.setPassword("");
        }
        if (conn.getIsDefault() != null && conn.getIsDefault() == 1) {
            dbConnMapper.clearDefault();
        } else {
            conn.setIsDefault(0);
        }
        dbConnMapper.insert(conn);
        return ApiResponse.ok(conn);
    }

    /** 删除连接 */
    @DeleteMapping("/{id}")
    public ApiResponse<Map<String, Object>> delete(@PathVariable long id) {
        dbConnMapper.deleteById(id);
        return ApiResponse.ok(Map.of("deleted", id));
    }

    /** 设为默认 */
    @PostMapping("/{id}/default")
    public ApiResponse<Map<String, Object>> setDefault(@PathVariable long id) {
        dbConnMapper.clearDefault();
        dbConnMapper.setDefault(id);
        return ApiResponse.ok(Map.of("default", id));
    }

    /** 测试连接（可选传入 SQL 试跑 EXPLAIN） */
    @PostMapping("/test")
    public ApiResponse<Map<String, Object>> test(@RequestBody Map<String, String> body) {
        String url = body.get("dbUrl");
        String user = body.get("username");
        String pass = body.get("password");
        String err = dbExplainService.testConnection(url, user, pass);
        Map<String, Object> result = new java.util.HashMap<>();
        if (err != null) {
            result.put("ok", false);
            result.put("error", err);
        } else {
            result.put("ok", true);
            String sql = body.get("sqlText");
            if (sql != null && !sql.isBlank()) {
                Map<String, Object> explain = dbExplainService.analyzeSql(url, user, pass, sql);
                result.put("explain", explain);
            }
        }
        return ApiResponse.ok(result);
    }
}
