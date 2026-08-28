package com.perfserver.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 数据库只读检测服务：对慢 SQL 执行 EXPLAIN（执行计划）、SHOW INDEX（索引清单）、
 * 表行数估算，作为大模型根因分析的量化证据。
 *
 * <p>安全约束：
 * <ul>
 *   <li>仅允许 SELECT/WITH 开头的单条语句做 EXPLAIN（EXPLAIN 只编译不执行，无副作用）；
 *       禁止 ANALYZE 关键字（EXPLAIN ANALYZE 会真实执行）</li>
 *   <li>禁止分号/注释，防多语句与注释绕过</li>
 *   <li>JDBC URL 仅允许 jdbc:mysql，连接/查询超时 3s/5s，失败不影响分析流程</li>
 * </ul>
 */
@Service
public class DbExplainService {

    private static final Logger log = LoggerFactory.getLogger(DbExplainService.class);

    /** SQL 中提取表名：FROM/JOIN/INTO/UPDATE 后的表名 */
    private static final Pattern TABLE_PATTERN =
            Pattern.compile("(?:from|join|into|update)\\s+`?([A-Za-z0-9_.]+)`?", Pattern.CASE_INSENSITIVE);

    private final ObjectMapper objectMapper = new ObjectMapper();

    /**
     * 对一条 SQL 执行完整只读检测。
     *
     * @return 检测结果：explainRows / explainJson / indexes / tableRows / error
     */
    public Map<String, Object> analyzeSql(String dbUrl, String username, String password, String sqlText) {
        Map<String, Object> result = new LinkedHashMap<>();
        // 先清理驱动 toString 前缀噪音，再校验
        String sql = sanitizeSql(sqlText == null ? "" : sqlText);
        String check = validate(dbUrl, sql);
        if (check != null) {
            result.put("error", check);
            return result;
        }
        try (Connection conn = openConnection(dbUrl, username, password)) {
            // 1. EXPLAIN FORMAT=JSON（含 cost/rows/possible_keys，MySQL 与 MariaDB 均支持），失败回退表格模式
            String explainJson = explainJson(conn, sql);
            if (explainJson != null) {
                result.put("explainJson", trim(explainJson, 4000));
            } else {
                result.put("explainRows", explainRows(conn, sql));
            }
            // 2. 涉及表的索引与规模
            List<String> tables = extractTables(sql);
            Map<String, String> indexes = new LinkedHashMap<>();
            Map<String, Long> tableRows = new LinkedHashMap<>();
            for (String table : tables) {
                String idx = showIndex(conn, table);
                if (idx != null) {
                    indexes.put(table, idx);
                }
                Long rows = tableRowCount(conn, table);
                if (rows != null) {
                    tableRows.put(table, rows);
                }
            }
            result.put("indexes", indexes);
            result.put("tableRows", tableRows);
            result.put("explainTables", tables);
            // 连接成功但执行计划/索引/表规模全部为空：极可能连错了库（表不存在），必须留痕
            Object rowsObj = result.get("explainRows");
            if (explainJson == null && (rowsObj == null || ((List<?>) rowsObj).isEmpty())
                    && indexes.isEmpty() && tableRows.isEmpty()) {
                result.put("error", "连接成功但未取得执行计划/索引/表规模——目标库中可能不存在相关表，"
                        + "请核对连接指向的库是否为慢 SQL 实际执行的库");
            }
        } catch (Exception e) {
            log.warn("db explain failed, url={}: {}", maskUrl(dbUrl), e.getMessage());
            result.put("error", "数据库检测失败: " + e.getMessage());
        }
        return result;
    }

    /** 从 JDBC URL 提取 host:port/db 标识（与 agent 端 dbHost 提取逻辑一致），失败返回空串 */
    public String urlHost(String dbUrl) {
        if (dbUrl == null) {
            return "";
        }
        int idx = dbUrl.indexOf("://");
        if (idx < 0) {
            return "";
        }
        String rest = dbUrl.substring(idx + 3);
        int q = rest.indexOf('?');
        return q > 0 ? rest.substring(0, q) : rest;
    }

    /** 测试连接是否可用，返回错误信息或 null（成功） */
    public String testConnection(String dbUrl, String username, String password) {
        String check = validate(dbUrl, "SELECT 1");
        if (check != null) {
            return check;
        }
        try (Connection conn = openConnection(dbUrl, username, password)) {
            try (Statement st = conn.createStatement()) {
                st.setQueryTimeout(3);
                try (ResultSet rs = st.executeQuery("SELECT VERSION()")) {
                    rs.next();
                    return null;
                }
            }
        } catch (Exception e) {
            return "连接失败: " + e.getMessage();
        }
    }

    // ===================== 内部实现 =====================

    /** 校验 URL 与 SQL 合法性，非法返回原因（自动清理驱动 toString 前缀噪音） */
    private String validate(String dbUrl, String sqlText) {
        if (dbUrl == null || dbUrl.isBlank()) {
            return "未配置数据库连接";
        }
        String url = dbUrl.trim();
        if (!url.startsWith("jdbc:mysql://") && !url.startsWith("jdbc:mariadb://")) {
            return "JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:mariadb://";
        }
        if (sqlText == null || sqlText.isBlank()) {
            return "SQL 为空";
        }
        String lower = sqlText.toLowerCase();
        if (!(lower.startsWith("select") || lower.startsWith("with"))) {
            return "仅支持对 SELECT/WITH 查询执行 EXPLAIN";
        }
        if (sqlText.contains(";") || sqlText.contains("--") || sqlText.contains("/*")) {
            return "SQL 含分号或注释，疑似多语句，已拒绝";
        }
        if (lower.contains("explain analyze") || lower.contains("into outfile") || lower.contains("into dumpfile")) {
            return "SQL 含受限关键字";
        }
        if (sqlText.length() > 4000) {
            return "SQL 过长";
        }
        return null;
    }

    /**
     * 清理 SQL 文本：剥掉驱动 toString 产生的前缀噪音（如 "&lt;prepared&gt; ...ClientPreparedStatement: "），
     * 定位到真正的 SQL 关键字（SELECT/INSERT/UPDATE/DELETE/WITH）起始处。
     */
    private String sanitizeSql(String sqlText) {
        String s = sqlText.trim();
        Matcher m = Pattern.compile(
                "(select|insert|update|delete|with)\\s", Pattern.CASE_INSENSITIVE).matcher(s);
        if (m.find() && m.start() > 0) {
            s = s.substring(m.start());
        }
        return s.trim();
    }

    private Connection openConnection(String dbUrl, String username, String password) throws Exception {
        String url = dbUrl.trim();
        if (!url.contains("?")) {
            url += "?connectTimeout=3000&socketTimeout=5000";
        }
        Properties props = new Properties();
        props.put("user", username == null ? "" : username);
        props.put("password", password == null ? "" : password);
        props.put("connectTimeout", "3000");
        DriverManager.setLoginTimeout(3);
        Connection conn = DriverManager.getConnection(url, props);
        conn.setReadOnly(true);
        return conn;
    }

    /** EXPLAIN FORMAT=JSON，失败返回 null */
    private String explainJson(Connection conn, String sql) {
        try (PreparedStatement ps = conn.prepareStatement("EXPLAIN FORMAT=JSON " + sql)) {
            ps.setQueryTimeout(5);
            try (ResultSet rs = ps.executeQuery()) {
                if (rs.next()) {
                    return rs.getString(1);
                }
            }
        } catch (Exception e) {
            log.debug("EXPLAIN FORMAT=JSON fallback: {}", e.getMessage());
        }
        return null;
    }

    /** 表格模式 EXPLAIN（兜底） */
    private List<Map<String, String>> explainRows(Connection conn, String sql) {
        List<Map<String, String>> rows = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement("EXPLAIN " + sql)) {
            ps.setQueryTimeout(5);
            try (ResultSet rs = ps.executeQuery()) {
                ResultSetMetaData md = rs.getMetaData();
                while (rs.next()) {
                    Map<String, String> row = new LinkedHashMap<>();
                    for (int i = 1; i <= md.getColumnCount(); i++) {
                        row.put(md.getColumnLabel(i), rs.getString(i));
                    }
                    rows.add(row);
                }
            }
        } catch (Exception e) {
            log.warn("EXPLAIN failed: {}", e.getMessage());
        }
        return rows;
    }

    /** SHOW INDEX 摘要：idx_name(col1,col2) [UNIQUE]，失败返回 null */
    private String showIndex(Connection conn, String table) {
        if (!table.matches("[A-Za-z0-9_.]+")) {
            return null;
        }
        Map<String, List<String>> idxCols = new LinkedHashMap<>();
        Map<String, Integer> idxUnique = new LinkedHashMap<>();
        try (Statement st = conn.createStatement()) {
            st.setQueryTimeout(3);
            try (ResultSet rs = st.executeQuery("SHOW INDEX FROM `" + table.replace("`", "") + "`")) {
                while (rs.next()) {
                    String keyName = rs.getString("Key_name");
                    String col = rs.getString("Column_name");
                    int seq = rs.getInt("Seq_in_index");
                    int nonUnique = rs.getInt("Non_unique");
                    idxCols.computeIfAbsent(keyName, k -> new ArrayList<>()).add(seq + "\u0000" + col);
                    idxUnique.put(keyName, nonUnique);
                }
            }
        } catch (Exception e) {
            return null;
        }
        StringBuilder sb = new StringBuilder();
        for (var entry : idxCols.entrySet()) {
            entry.getValue().sort(String::compareTo);
            String cols = entry.getValue().stream().map(s -> s.substring(s.indexOf('\u0000') + 1))
                    .reduce((a, b) -> a + "," + b).orElse("");
            String flag = idxUnique.getOrDefault(entry.getKey(), 1) == 0 ? " [UNIQUE]" : "";
            if (sb.length() > 0) {
                sb.append("; ");
            }
            sb.append(entry.getKey()).append("(").append(cols).append(")").append(flag);
        }
        return sb.length() > 0 ? sb.toString() : "（无二级索引）";
    }

    /** 表行数估算（information_schema，不扫描真实数据），失败返回 null */
    private Long tableRowCount(Connection conn, String table) {
        String catalog;
        try {
            catalog = conn.getCatalog();
        } catch (Exception e) {
            catalog = null;
        }
        int dot = table.lastIndexOf('.');
        String schema = dot > 0 ? table.substring(0, dot) : catalog;
        String tableName = dot > 0 ? table.substring(dot + 1) : table;
        if (!tableName.matches("[A-Za-z0-9_]+") || !schema.matches("[A-Za-z0-9_]+")) {
            return null;
        }
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?")) {
            ps.setString(1, schema);
            ps.setString(2, tableName);
            ps.setQueryTimeout(3);
            try (ResultSet rs = ps.executeQuery()) {
                if (rs.next()) {
                    return rs.getLong(1);
                }
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    /** 提取 SQL 涉及表名（去重，最多 5 张） */
    private List<String> extractTables(String sql) {
        List<String> tables = new ArrayList<>();
        Matcher m = TABLE_PATTERN.matcher(sql);
        while (m.find() && tables.size() < 5) {
            String t = m.group(1);
            if (!tables.contains(t)) {
                tables.add(t);
            }
        }
        return tables;
    }

    /** 供大模型使用的紧凑文本摘要 */
    public String formatSummary(Map<String, Object> explain) {
        if (explain == null || explain.isEmpty()) {
            return "";
        }
        StringBuilder sb = new StringBuilder();
        Object error = explain.get("error");
        if (error != null) {
            sb.append("（数据库检测不可用：").append(error).append("）");
            return sb.toString();
        }
        Object rows = explain.get("explainRows");
        if (rows instanceof List<?> list && !list.isEmpty()) {
            sb.append("EXPLAIN 执行计划：\n");
            for (Object r : list) {
                sb.append("- ").append(r).append("\n");
            }
        }
        Object json = explain.get("explainJson");
        if (json != null) {
            sb.append("EXPLAIN JSON（含 cost/rows/keys）：\n").append(json).append("\n");
        }
        Object indexes = explain.get("indexes");
        if (indexes instanceof Map<?, ?> idxMap && !idxMap.isEmpty()) {
            sb.append("现有索引：\n");
            idxMap.forEach((t, v) -> sb.append("- ").append(t).append(": ").append(v).append("\n"));
        }
        Object tableRows = explain.get("tableRows");
        if (tableRows instanceof Map<?, ?> trMap && !trMap.isEmpty()) {
            sb.append("表规模（估算行数）：");
            trMap.forEach((t, v) -> sb.append(t).append("=").append(v).append(" "));
            sb.append("\n");
        }
        return sb.toString();
    }

    private String maskUrl(String url) {
        return url == null ? "" : url.replaceAll("password=[^&]*", "password=***");
    }

    private String trim(String s, int max) {
        return s.length() <= max ? s : s.substring(0, max) + "...(截断)";
    }
}
