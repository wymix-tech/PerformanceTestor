package com.perftestor.datagen.spi;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;

/**
 * 数据库方言 SPI：元数据读取、枚举值查询、SQL 构建与标识符引用全部下沉，
 * 解除核心引擎对 MySQL 的硬依赖。本期实现 MySQLDialect；
 * 未来 PostgreSQL（pg_enum/双引号/COPY）等仅需新增实现类。
 */
public interface DatabaseDialect {

    /** 是否支持该 JDBC URL（如 jdbc:mysql:// 前缀） */
    boolean supports(String jdbcUrl);

    /** URL 增强（如 MySQL 追加 rewriteBatchedStatements=true；其他方言原样返回） */
    String enrichJdbcUrl(String jdbcUrl);

    /**
     * 读取表元数据：列（含类型/注释/自增）、主键、声明外键、当前行数。
     * 表名/列名内部统一小写。
     */
    com.perftestor.datagen.model.SchemaMetadata fetchMetadata(Connection conn, String tableName) throws SQLException;

    /**
     * 枚举值查询：MySQL 列内 ENUM（解析 TYPE_NAME）；PG 独立对象（查 pg_enum）。
     * 统一返回候选值列表；非枚举列返回空列表。
     */
    List<String> getEnumValues(Connection conn, String tableName, String columnName) throws SQLException;

    /** 标识符引用：MySQL 反引号 `name` / PG 双引号 "name" */
    String quoteIdentifier(String name);

    /** 构建 INSERT 语句（方言化列引用；值一律占位符 ?） */
    String buildInsertSql(String tableName, List<String> columns);

    /** 精确行数或估算行数（-1 表示无法获取） */
    default long tableRowCount(Connection conn, String tableName) throws SQLException {
        return -1L;
    }
}
