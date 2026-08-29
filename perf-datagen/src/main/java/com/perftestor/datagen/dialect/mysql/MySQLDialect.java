package com.perftestor.datagen.dialect.mysql;

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
 * MySQL 方言实现：承接原 DataGenService 的 MySQL 特有逻辑。
 * URL 增强 rewriteBatchedStatements、useInformationSchema（读列注释）、
 * getImportedKeys 外键读取、TYPE_NAME 内联 ENUM 解析、反引号标识符。
 */
public class MySQLDialect implements DatabaseDialect {

    /** ENUM('A','B') / SET('X') 类型名解析 */
    private static final Pattern QUOTED = Pattern.compile("'([^']*)'");

    @Override
    public boolean supports(String jdbcUrl) {
        return jdbcUrl != null && jdbcUrl.toLowerCase(Locale.ROOT).startsWith("jdbc:mysql://");
    }

    @Override
    public String enrichJdbcUrl(String jdbcUrl) {
        if (jdbcUrl.contains("rewriteBatchedStatements")) {
            return jdbcUrl;
        }
        return jdbcUrl + (jdbcUrl.contains("?") ? "&" : "?")
                + "rewriteBatchedStatements=true&useInformationSchema=true";
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
                meta.getPrimaryKeys().add(pk.toLowerCase(Locale.ROOT));
                for (SchemaMetadata.ColumnMeta c : meta.getColumns()) {
                    if (c.getName().equalsIgnoreCase(pk)) {
                        c.setPrimaryKey(true);
                    }
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
        // 行数（快速 COUNT；失败置 -1 不阻断）
        meta.setRowCount(tableRowCount(conn, tableName));
        return meta;
    }

    @Override
    public long tableRowCount(Connection conn, String tableName) {
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT COUNT(*) FROM `" + tableName.replace("`", "") + "`");
             ResultSet rs = ps.executeQuery()) {
            return rs.next() ? rs.getLong(1) : -1L;
        } catch (SQLException e) {
            return -1L;
        }
    }

    @Override
    public List<String> getEnumValues(Connection conn, String tableName, String columnName) {
        // MySQL 列内 ENUM 由 TYPE_NAME 解析（fetchMetadata 已带 typeName）；
        // 此处按表+列重查一次 TYPE_NAME 供独立调用场景使用。
        List<String> vals = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                        + "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND COLUMN_NAME = ?")) {
            ps.setString(1, tableName);
            ps.setString(2, columnName);
            try (ResultSet rs = ps.executeQuery()) {
                if (rs.next()) {
                    String type = rs.getString(1);
                    if (type != null && type.toUpperCase(Locale.ROOT).startsWith("ENUM")) {
                        Matcher m = QUOTED.matcher(type);
                        while (m.find()) {
                            vals.add(m.group(1));
                        }
                    }
                }
            }
        } catch (SQLException ignore) {
            // 降级：返回空列表，由后续枚举来源接管
        }
        return vals;
    }

    @Override
    public String quoteIdentifier(String name) {
        return "`" + name.replace("`", "``") + "`";
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
}
