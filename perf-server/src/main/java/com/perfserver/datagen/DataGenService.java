package com.perfserver.datagen;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.DataGenTask;
import com.perfserver.mapper.DataGenTaskMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.sql.Connection;
import java.sql.DatabaseMetaData;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadLocalRandom;

/**
 * 数据灌入服务：读取目标表结构（DatabaseMetaData）→ 类型映射生成测试数据 →
 * rewriteBatchedStatements 批量插入。支持字段级自定义规则覆盖默认生成器。
 *
 * <p>安全：JDBC URL 仅允许 jdbc:mysql:// 前缀；表名白名单正则；行数上限 100 万；
 * 批量大小 500，异步执行并实时更新进度。
 */
@Service
public class DataGenService {

    private static final Logger log = LoggerFactory.getLogger(DataGenService.class);

    private static final int BATCH_SIZE = 500;
    private static final int MAX_ROWS = 1_000_000;
    private static final String TABLE_NAME_REGEX = "^[A-Za-z0-9_]+$";

    private static final ExecutorService DATAGEN_EXECUTOR = Executors.newFixedThreadPool(2, r -> {
        Thread t = new Thread(r, "datagen-runner");
        t.setDaemon(true);
        return t;
    });

    private final DataGenTaskMapper taskMapper;
    private final ObjectMapper objectMapper;

    public DataGenService(DataGenTaskMapper taskMapper, ObjectMapper objectMapper) {
        this.taskMapper = taskMapper;
        this.objectMapper = objectMapper;
    }

    /** 创建任务并异步执行 */
    public DataGenTask createAndRun(DataGenRequest request) {
        validate(request);
        DataGenTask task = new DataGenTask();
        task.setTaskName(request.getTaskName() == null || request.getTaskName().isBlank()
                ? "灌数据-" + request.getTableName() : request.getTaskName());
        task.setDbUrl(request.getDbUrl());
        task.setTableName(request.getTableName());
        task.setRowCount(request.getRowCount());
        task.setRuleJson(request.getRuleJson());
        task.setStatus("pending");
        task.setInserted(0);
        taskMapper.insert(task);

        final long taskId = task.getId();
        final DataGenRequest req = request;
        DATAGEN_EXECUTOR.submit(() -> {
            long start = System.currentTimeMillis();
            try {
                taskMapper.updateStatus(taskId, "running");
                int inserted = doGenerate(req, taskId);
                taskMapper.updateFinished(taskId, "finished", inserted,
                        System.currentTimeMillis() - start, null);
                log.info("datagen finished, taskId={}, table={}, rows={}", taskId, req.getTableName(), inserted);
            } catch (Exception e) {
                log.error("datagen failed, taskId={}", taskId, e);
                taskMapper.updateFinished(taskId, "failed", 0,
                        System.currentTimeMillis() - start, e.getMessage());
            }
        });
        return task;
    }

    /** 核心生成与批量插入 */
    private int doGenerate(DataGenRequest request, long taskId) throws Exception {
        String url = ensureRewriteBatch(request.getDbUrl());
        Properties props = new Properties();
        props.setProperty("user", request.getDbUser() == null ? "" : request.getDbUser());
        props.setProperty("password", request.getDbPassword() == null ? "" : request.getDbPassword());
        props.setProperty("connectTimeout", "5000");
        props.setProperty("socketTimeout", "60000");

        List<ColumnInfo> columns;
        try (Connection conn = DriverManager.getConnection(url, props)) {
            columns = readColumns(conn, request.getTableName());
            if (columns.isEmpty()) {
                throw new IllegalArgumentException("表不存在或无可见列: " + request.getTableName());
            }
            RuleSet rules = parseRules(request.getRuleJson());
            List<ColumnInfo> insertColumns = new ArrayList<>();
            for (ColumnInfo col : columns) {
                if (!col.isAutoIncrement()) {
                    insertColumns.add(col);
                }
            }
            StringBuilder sql = new StringBuilder("INSERT INTO ")
                    .append(request.getTableName()).append(" (");
            for (int i = 0; i < insertColumns.size(); i++) {
                if (i > 0) {
                    sql.append(", ");
                }
                sql.append('`').append(insertColumns.get(i).getName()).append('`');
            }
            sql.append(") VALUES (");
            for (int i = 0; i < insertColumns.size(); i++) {
                if (i > 0) {
                    sql.append(", ");
                }
                sql.append('?');
            }
            sql.append(')');

            int inserted = 0;
            try (PreparedStatement ps = conn.prepareStatement(sql.toString())) {
                conn.setAutoCommit(false);
                for (int row = 0; row < request.getRowCount(); row++) {
                    for (int i = 0; i < insertColumns.size(); i++) {
                        ColumnInfo col = insertColumns.get(i);
                        setValue(ps, i + 1, col, rules);
                    }
                    ps.addBatch();
                    if ((row + 1) % BATCH_SIZE == 0) {
                        inserted += ps.executeBatch().length;
                        conn.commit();
                        taskMapper.updateProgress(taskId, inserted);
                    }
                }
                if (request.getRowCount() % BATCH_SIZE != 0) {
                    inserted += ps.executeBatch().length;
                    conn.commit();
                }
                taskMapper.updateProgress(taskId, inserted);
            }
            return inserted;
        }
    }

    /** 读取表结构（跳过系统表） */
    private List<ColumnInfo> readColumns(Connection conn, String tableName) throws Exception {
        List<ColumnInfo> columns = new ArrayList<>();
        DatabaseMetaData meta = conn.getMetaData();
        String catalog = conn.getCatalog();
        try (ResultSet rs = meta.getColumns(catalog, null, tableName, "%")) {
            while (rs.next()) {
                ColumnInfo col = new ColumnInfo();
                col.setName(rs.getString("COLUMN_NAME"));
                col.setTypeName(rs.getString("TYPE_NAME"));
                col.setSize(rs.getInt("COLUMN_SIZE"));
                col.setNullable(rs.getInt("NULLABLE") != DatabaseMetaData.columnNoNulls);
                col.setAutoIncrement("YES".equalsIgnoreCase(rs.getString("IS_AUTOINCREMENT")));
                columns.add(col);
            }
        }
        // 主键列标记
        try (ResultSet rs = meta.getPrimaryKeys(catalog, null, tableName)) {
            while (rs.next()) {
                String pk = rs.getString("COLUMN_NAME");
                for (ColumnInfo col : columns) {
                    if (col.getName().equalsIgnoreCase(pk)) {
                        col.setPrimaryKey(true);
                    }
                }
            }
        }
        return columns;
    }

    /** 按列类型 + 规则设置参数值 */
    private void setValue(PreparedStatement ps, int idx, ColumnInfo col, RuleSet rules) throws Exception {
        JsonNode rule = rules.get(col.getName());
        Object value = rule != null ? applyRule(rule, col) : generateDefault(col);
        if (value == null) {
            ps.setObject(idx, null);
            return;
        }
        switch (col.getTypeName().toUpperCase()) {
            case "INT", "INTEGER", "SMALLINT", "TINYINT", "MEDIUMINT", "YEAR" ->
                    ps.setInt(idx, ((Number) value).intValue());
            case "BIGINT" -> ps.setLong(idx, ((Number) value).longValue());
            case "FLOAT" -> ps.setFloat(idx, ((Number) value).floatValue());
            case "DOUBLE" -> ps.setDouble(idx, ((Number) value).doubleValue());
            case "DECIMAL", "NUMERIC" -> ps.setBigDecimal(idx, new java.math.BigDecimal(value.toString()));
            case "DATE" -> ps.setDate(idx, java.sql.Date.valueOf(((LocalDateTime) value).toLocalDate()));
            case "DATETIME", "TIMESTAMP" -> ps.setTimestamp(idx, java.sql.Timestamp.valueOf((LocalDateTime) value));
            case "BIT", "BOOLEAN", "BOOL" -> ps.setBoolean(idx, ((Number) value).intValue() != 0);
            default -> ps.setString(idx, String.valueOf(value));
        }
    }

    /** 默认类型映射生成器 */
    private Object generateDefault(ColumnInfo col) {
        ThreadLocalRandom r = ThreadLocalRandom.current();
        String type = col.getTypeName().toUpperCase();
        return switch (type) {
            case "INT", "INTEGER", "SMALLINT", "TINYINT", "MEDIUMINT", "YEAR" -> r.nextInt(1, 100000);
            case "BIGINT" -> Math.abs(r.nextLong());
            case "FLOAT", "DOUBLE" -> Math.round(r.nextDouble() * 10000) / 100.0;
            case "DECIMAL", "NUMERIC" -> Math.round(r.nextDouble() * 100000) / 100.0;
            case "DATE", "DATETIME", "TIMESTAMP" -> LocalDateTime.now().minusDays(r.nextInt(0, 365)).withNano(0);
            case "BIT", "BOOLEAN", "BOOL" -> r.nextInt(0, 2);
            case "TEXT", "MEDIUMTEXT", "LONGTEXT", "CLOB" -> randomText(Math.min(col.getSize(), 200));
            default -> randomText(Math.max(1, Math.min(col.getSize(), 64)));
        };
    }

    /** 规则生成器：fixed / range / enum */
    private Object applyRule(JsonNode rule, ColumnInfo col) {
        ThreadLocalRandom r = ThreadLocalRandom.current();
        String type = rule.path("type").asText("random");
        switch (type) {
            case "fixed" -> {
                JsonNode v = rule.path("value");
                if (v.isNumber()) {
                    return v.numberValue();
                }
                return v.asText();
            }
            case "range" -> {
                double min = rule.path("min").asDouble(0);
                double max = rule.path("max").asDouble(100);
                return Math.round((min + r.nextDouble() * (max - min)) * 100) / 100.0;
            }
            case "enum" -> {
                JsonNode values = rule.path("values");
                if (values.isArray() && values.size() > 0) {
                    JsonNode v = values.get(r.nextInt(values.size()));
                    return v.isNumber() ? v.numberValue() : v.asText();
                }
                return generateDefault(col);
            }
            default -> {
                return generateDefault(col);
            }
        }
    }

    private String randomText(int len) {
        String chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
        StringBuilder sb = new StringBuilder(len);
        ThreadLocalRandom r = ThreadLocalRandom.current();
        for (int i = 0; i < len; i++) {
            sb.append(chars.charAt(r.nextInt(chars.length())));
        }
        return sb.toString();
    }

    /** 解析规则 JSON → Map<列名, 规则节点> */
    private RuleSet parseRules(String ruleJson) {
        RuleSet set = new RuleSet();
        if (ruleJson == null || ruleJson.isBlank()) {
            return set;
        }
        try {
            JsonNode root = objectMapper.readTree(ruleJson);
            JsonNode fields = root.path("fields");
            if (fields.isObject()) {
                fields.fields().forEachRemaining(e -> set.put(e.getKey(), e.getValue()));
            }
        } catch (Exception e) {
            log.warn("parse datagen rules failed: {}", ruleJson, e);
        }
        return set;
    }

    /** 任务列表（时间倒序） */
    public List<DataGenTask> list(int limit) {
        return taskMapper.query(Math.max(1, Math.min(limit, 200)));
    }

    /** 任务详情 */
    public DataGenTask detail(long id) {
        return taskMapper.findById(id);
    }

    /** 目标库表清单（灌数据表单下拉） */
    public List<String> listTables(String dbUrl, String dbUser, String dbPassword) {
        validateDbUrl(dbUrl);
        List<String> tables = new ArrayList<>();
        try (Connection conn = connect(dbUrl, dbUser, dbPassword);
             ResultSet rs = conn.getMetaData().getTables(conn.getCatalog(), null, "%",
                     new String[]{"TABLE"})) {
            while (rs.next()) {
                tables.add(rs.getString("TABLE_NAME"));
            }
        } catch (Exception e) {
            throw new IllegalArgumentException("连接目标库失败: " + e.getMessage());
        }
        return tables;
    }

    /** 目标表列清单（规则配置表单） */
    public List<ColumnInfo> listColumns(String dbUrl, String dbUser, String dbPassword, String tableName) {
        validateDbUrl(dbUrl);
        validateTableName(tableName);
        try (Connection conn = connect(dbUrl, dbUser, dbPassword)) {
            return readColumns(conn, tableName);
        } catch (Exception e) {
            throw new IllegalArgumentException("读取表结构失败: " + e.getMessage());
        }
    }

    private Connection connect(String dbUrl, String dbUser, String dbPassword) throws Exception {
        Properties props = new Properties();
        props.setProperty("user", dbUser == null ? "" : dbUser);
        props.setProperty("password", dbPassword == null ? "" : dbPassword);
        props.setProperty("connectTimeout", "5000");
        return DriverManager.getConnection(ensureRewriteBatch(dbUrl), props);
    }

    /** 追加 rewriteBatchedStatements 提升批量插入性能 */
    private String ensureRewriteBatch(String url) {
        if (url.contains("rewriteBatchedStatements")) {
            return url;
        }
        return url + (url.contains("?") ? "&" : "?") + "rewriteBatchedStatements=true";
    }

    /** 参数校验与上限保护 */
    private void validate(DataGenRequest request) {
        validateDbUrl(request.getDbUrl());
        validateTableName(request.getTableName());
        if (request.getRowCount() < 1 || request.getRowCount() > MAX_ROWS) {
            throw new IllegalArgumentException("行数必须在 1-" + MAX_ROWS + " 之间");
        }
    }

    private void validateDbUrl(String dbUrl) {
        if (dbUrl == null || !dbUrl.startsWith("jdbc:mysql://")) {
            throw new IllegalArgumentException("JDBC URL 仅支持 jdbc:mysql:// 前缀");
        }
    }

    private void validateTableName(String tableName) {
        if (tableName == null || !tableName.matches(TABLE_NAME_REGEX)) {
            throw new IllegalArgumentException("表名仅允许字母数字下划线");
        }
    }

    /** 规则集合（列名 → 规则 JSON 节点） */
    private static class RuleSet {
        private final Map<String, JsonNode> rules = new HashMap<>();

        void put(String column, JsonNode rule) {
            rules.put(column, rule);
        }

        JsonNode get(String column) {
            return rules.get(column);
        }
    }
}
