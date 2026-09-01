package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.DbConn;
import com.perfserver.mapper.DbConnMapper;
import com.perfserver.security.CryptoService;
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
 *
 * <p>凭据处理约定：
 * <ul>
 *   <li>保存：密码经 {@link CryptoService} 加密后落库；</li>
 *   <li>列表：SQL 层即不查凭据列，返回体只带 {@code hasPassword} 标记；</li>
 *   <li>测试：不传 password 时按 id 从库内取凭据并解密，明文不出服务端。</li>
 * </ul>
 */
@RestController
@RequestMapping("/api/dbconns")
public class DbConnController {

    private final DbConnMapper dbConnMapper;
    private final DbExplainService dbExplainService;
    private final CryptoService cryptoService;

    public DbConnController(DbConnMapper dbConnMapper, DbExplainService dbExplainService,
                            CryptoService cryptoService) {
        this.dbConnMapper = dbConnMapper;
        this.dbExplainService = dbExplainService;
        this.cryptoService = cryptoService;
    }

    /**
     * 连接列表（不含凭据，仅 hasPassword 标记）。
     *
     * <p>SQL 只查非敏感列 + {@code has_password} 计算列，凭据内容根本不进入结果集；
     * 因此这里不能再基于 password 字段二次赋值——该字段恒为 null，会覆盖掉正确的计算值。
     */
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
        // 加密落库；存量明文在下次保存时自动转为密文
        conn.setPassword(cryptoService.encrypt(conn.getPassword()));
        dbConnMapper.insert(conn);
        // 回执脱敏，避免明文经响应体回显
        conn.setHasPassword(cryptoService.hasSecret(conn.getPassword()));
        conn.setPassword(cryptoService.mask(conn.getPassword()));
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

    /**
     * 按 ID 解析连接；ID 非数字时不抛异常（返回 null）。
     *
     * <p>connId 来自请求体字符串，直接 {@code Long.parseLong} 会因非法输入抛
     * {@link NumberFormatException} 变成 500——测试连接是辅助功能，
     * 非法输入应退回「按手填凭据测试」而非报错。
     */
    private DbConn findConnByIdOrNull(String connId) {
        try {
            return dbConnMapper.findById(Long.parseLong(connId.trim()));
        } catch (NumberFormatException e) {
            return null;
        }
    }

    /**
     * 测试连接（可选传入 SQL 试跑 EXPLAIN）。
     *
     * <p>body 中的 password 为前端手填值（新增连接前探活，不落库）；
     * 传 connId 且不带 password 时，改用库内已保存凭据（解密后使用，明文不出服务端）。
     */
    @PostMapping("/test")
    public ApiResponse<Map<String, Object>> test(@RequestBody Map<String, String> body) {
        String url = body.get("dbUrl");
        String user = body.get("username");
        String pass = body.get("password");
        String connId = body.get("connId");
        if ((pass == null || pass.isBlank()) && connId != null && !connId.isBlank()) {
            DbConn saved = findConnByIdOrNull(connId);
            if (saved != null) {
                url = saved.getDbUrl();
                user = saved.getUsername();
                pass = cryptoService.decrypt(saved.getPassword());
            }
        }
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
