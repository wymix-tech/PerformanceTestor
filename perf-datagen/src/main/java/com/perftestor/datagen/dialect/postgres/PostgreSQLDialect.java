package com.perftestor.datagen.dialect.postgres;

import com.perftestor.datagen.model.SchemaMetadata;
import com.perftestor.datagen.spi.DatabaseDialect;

import java.sql.Connection;
import java.sql.DatabaseMetaData;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * PostgreSQL 方言实现：与 {@code MySQLDialect} 逐方法对齐。
 *
 * <p>差异点：
 * <ul>
 *   <li>URL：{@code jdbc:postgresql:}；增强参数 {@code reWriteBatchedInserts=true}
 *       （等价于 MySQL 的 rewriteBatchedStatements，多行 VALUES 合并）。</li>
 *   <li>元数据：PG 无 {@code SHOW TABLES} / {@code TYPE_NAME ENUM}，统一走
 *       JDBC {@code DatabaseMetaData} + {@code information_schema}（PG 原生即标准实现）。</li>
 *   <li>枚举发现：原生枚举类型查 {@code pg_enum}（information_schema.udt_name 关联），
 *       降级走 CHECK 约束（{@code pg_get_constraintdef} 文本解析）。</li>
 *   <li>SERIAL/BIGSERIAL：主键默认值 {@code nextval(...)} 识别为自增，跳过插值。</li>
 *   <li>标识符：双引号 {@code "name"}（PG 大小写敏感，未加引号的标识符折成小写）。</li>
 * </ul>
 *
 * <p>容错：所有 information_schema / pg_catalog 查询失败均静默降级（返回空或部分结果），
 * 绝不因元数据缺失中断灌数流程。
 */
public class PostgreSQLDialect implements DatabaseDialect {

    /** PG 约束定义中的字符串字面量 */
    private static final Pattern QUOTED = Pattern.compile("'([^']*)'");
    /** PG 约束定义中的类型强制转换（::text / ::character varying(32) / ::numeric(10,2)） */
    private static final Pattern CAST = Pattern.compile("::\\s*[A-Za-z_][A-Za-z0-9_\\s]*(\\(\\s*\\d+\\s*(,\\s*\\d+\\s*)?\\))?");
    /** 数值枚举：CHECK (col IN (1, 2, 3)) */
    private static final Pattern IN_LIST = Pattern.compile("\\bin\\s*\\(([^)]+)\\)", Pattern.CASE_INSENSITIVE);

    @Override
    public boolean supports(String jdbcUrl) {
        return jdbcUrl != null && jdbcUrl.toLowerCase(Locale.ROOT).startsWith("jdbc:postgresql:");
    }

    @Override
    public String enrichJdbcUrl(String jdbcUrl) {
        StringBuilder url = new StringBuilder(jdbcUrl);
        // reWriteBatchedInserts：多行 VALUES 合并（等价 MySQL rewriteBatchedStatements）
        if (!jdbcUrl.contains("reWriteBatchedInserts")) {
            url.append(jdbcUrl.contains("?") ? '&' : '?').append("reWriteBatchedInserts=true");
        }
        // stringtype=unspecified：参数以 unknown 类型发送，由 PG 按目标列类型推断。
        // 必需——否则字符串参数一律按 varchar 发送，写入原生枚举/JSON 等列会直接报
        // "column X is of type e2e_status but expression is of type character varying"。
        if (!jdbcUrl.contains("stringtype")) {
            url.append(url.indexOf("?") >= 0 ? '&' : '?').append("stringtype=unspecified");
        }
        return url.toString();
    }

    @Override
    public SchemaMetadata fetchMetadata(Connection conn, String tableName) throws SQLException {
        SchemaMetadata meta = new SchemaMetadata();
        meta.setTableName(tableName);
        DatabaseMetaData dmd = conn.getMetaData();
        String catalog = conn.getCatalog();
        try (ResultSet rs = dmd.getColumns(catalog, null, tableName, "%")) {
            while (rs.next()) {
                SchemaMetadata.ColumnMeta col = new SchemaMetadata.ColumnMeta();
                col.setName(rs.getString("COLUMN_NAME"));
                col.setTypeName(rs.getString("TYPE_NAME"));
                col.setSize(rs.getInt("COLUMN_SIZE"));
                col.setNullable(rs.getInt("NULLABLE") != DatabaseMetaData.columnNoNulls);
                col.setAutoIncrement("YES".equalsIgnoreCase(rs.getString("IS_AUTOINCREMENT")));
                col.setComment(rs.getString("REMARKS"));
                meta.getColumns().add(col);
            }
        }
        // 主键
        try (ResultSet rs = dmd.getPrimaryKeys(catalog, null, tableName)) {
            while (rs.next()) {
                String pk = rs.getString("COLUMN_NAME");
                if (pk == null) {
                    continue;
                }
                meta.getPrimaryKeys().add(pk.toLowerCase(Locale.ROOT));
                SchemaMetadata.ColumnMeta c = meta.findColumn(pk);
                if (c != null) {
                    c.setPrimaryKey(true);
                }
            }
        }
        // 声明外键（ImportedKeys：本表引用的父表）
        try (ResultSet rs = dmd.getImportedKeys(catalog, null, tableName)) {
            while (rs.next()) {
                meta.getForeignKeys().add(new SchemaMetadata.FkMeta(
                        rs.getString("FKCOLUMN_NAME"),
                        rs.getString("PKTABLE_NAME"),
                        rs.getString("PKCOLUMN_NAME")));
            }
        }
        // SERIAL/BIGSERIAL：默认值 nextval(...)（PG 驱动的 IS_AUTOINCREMENT 覆盖不全）
        markSerialDefaults(conn, meta, tableName);
        meta.setRowCount(tableRowCount(conn, tableName));
        return meta;
    }

    @Override
    public long tableRowCount(Connection conn, String tableName) {
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT COUNT(*) FROM " + quoteIdentifier(tableName));
             ResultSet rs = ps.executeQuery()) {
            return rs.next() ? rs.getLong(1) : -1L;
        } catch (SQLException e) {
            return -1L;
        }
    }

    @Override
    public List<String> getEnumValues(Connection conn, String tableName, String columnName) {
        List<String> vals = new ArrayList<>();
        if (conn == null || columnName == null || columnName.isBlank()) {
            return vals;
        }
        // 1) 原生枚举类型：information_schema.udt_name → pg_type → pg_enum
        vals.addAll(nativeEnumValues(conn, tableName, columnName));
        if (!vals.isEmpty()) {
            return vals;
        }
        // 2) CHECK 约束（PG 无列内 ENUM，CHECK 是最常见替代方案）
        return checkEnumValues(conn, tableName, columnName);
    }

    @Override
    public String quoteIdentifier(String name) {
        return "\"" + (name == null ? "" : name.replace("\"", "\"\"")) + "\"";
    }

    @Override
    public String buildInsertSql(String tableName, List<String> columns) {
        StringBuilder sql = new StringBuilder("INSERT INTO ").append(quoteIdentifier(tableName)).append(" (");
        for (int i = 0; i < columns.size(); i++) {
            if (i > 0) {
                sql.append(", ");
            }
            sql.append(quoteIdentifier(columns.get(i)));
        }
        sql.append(") VALUES (");
        for (int i = 0; i < columns.size(); i++) {
            if (i > 0) {
                sql.append(", ");
            }
            sql.append('?');
        }
        sql.append(')');
        return sql.toString();
    }

    /** SERIAL/BIGSERIAL 识别：列默认值为 nextval(...) 时标记自增（生成期跳过插值） */
    private void markSerialDefaults(Connection conn, SchemaMetadata meta, String tableName) {
        String sql = "SELECT COLUMN_NAME, COLUMN_DEFAULT FROM information_schema.COLUMNS WHERE TABLE_NAME = ?";
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            ps.setString(1, tableName);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    SchemaMetadata.ColumnMeta col = meta.findColumn(rs.getString(1));
                    if (col != null && isSerialDefault(rs.getString(2))) {
                        col.setAutoIncrement(true);
                    }
                }
            }
        } catch (SQLException ignore) {
            // 无 information_schema 访问权时降级：仅依赖 JDBC 元数据
        }
    }

    /** SERIAL/BIGSERIAL 判定：列默认值为 {@code nextval('..._id_seq'::regclass)} */
    static boolean isSerialDefault(String columnDefault) {
        return columnDefault != null
                && columnDefault.toLowerCase(Locale.ROOT).contains("nextval(");
    }

    /** 原生 PG 枚举类型：udt_name 关联 pg_type/pg_enum */
    private List<String> nativeEnumValues(Connection conn, String tableName, String columnName) {
        List<String> vals = new ArrayList<>();
        if (tableName == null || tableName.isBlank()) {
            return vals;
        }
        String sql = "SELECT e.enumlabel FROM information_schema.columns c "
                + "JOIN pg_type t ON t.typname = c.udt_name "
                + "JOIN pg_enum e ON e.enumtypid = t.oid "
                + "WHERE c.table_name = ? AND c.column_name = ? "
                + "ORDER BY e.enumsortorder";
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            ps.setString(1, tableName.toLowerCase(Locale.ROOT));
            ps.setString(2, columnName.toLowerCase(Locale.ROOT));
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    vals.add(rs.getString(1));
                }
            }
        } catch (SQLException ignore) {
            // 非 PG（如 H2 PG 兼容模式无 pg_enum）降级到 CHECK 约束发现
        }
        return vals;
    }

    /** CHECK 约束枚举发现：pg_get_constraintdef 文本解析 */
    private List<String> checkEnumValues(Connection conn, String tableName, String columnName) {
        List<String> vals = new ArrayList<>();
        if (tableName == null || tableName.isBlank()) {
            return vals;
        }
        String sql = "SELECT pg_get_constraintdef(con.oid) FROM pg_constraint con "
                + "JOIN pg_class rel ON rel.oid = con.conrelid "
                + "JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = ANY(con.conkey) "
                + "WHERE con.contype = 'c' AND rel.relname = ? AND att.attname = ?";
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            ps.setString(1, tableName.toLowerCase(Locale.ROOT));
            ps.setString(2, columnName.toLowerCase(Locale.ROOT));
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    for (String v : parseCheckDefinition(rs.getString(1))) {
                        if (!vals.contains(v)) {
                            vals.add(v);
                        }
                    }
                }
            }
        } catch (SQLException ignore) {
            // 无 pg_catalog 视图（H2 兼容模式）时降级：返回空列表交由后续枚举来源
        }
        return vals;
    }

    /**
     * 解析 PG 约束定义中的候选值：
     * {@code CHECK ((status = ANY (ARRAY['PENDING'::character varying, 'PAID'::character varying])))}
     * {@code CHECK ((status IN ('A', 'B')))} / {@code CHECK ((level IN (1, 2, 3)))}。
     */
    static List<String> parseCheckDefinition(String definition) {
        List<String> vals = new ArrayList<>();
        if (definition == null || definition.isBlank()) {
            return vals;
        }
        String s = CAST.matcher(definition).replaceAll("");
        Matcher m = QUOTED.matcher(s);
        while (m.find()) {
            String v = m.group(1);
            if (!vals.contains(v)) {
                vals.add(v);
            }
        }
        if (!vals.isEmpty()) {
            return vals;
        }
        Matcher in = IN_LIST.matcher(s);
        if (in.find()) {
            for (String part : in.group(1).split(",")) {
                String p = part.trim();
                if (!p.isEmpty() && !vals.contains(p)) {
                    vals.add(p);
                }
            }
        }
        return vals;
    }
}
