package com.perftestor.datagen.spi;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;

/**
 * 批量写入 SPI：不同数据库的最佳写入策略不同（MySQL JDBC Batch、PG COPY、Oracle ArrayBinding）。
 * 本期实现 JdbcBatchExecutor；PgCopyExecutor 等为未来扩展点。
 *
 * <p><b>性能警告（极高 refSkew 场景）</b>：若热点倾斜导致单热行承载 &gt;50% 子行，
 * 高并发压测时数据库行锁冲突会抵消批量写入收益。建议：降低 BATCH_SIZE（如 500→200）、
 * 为子表开启分区，或在业务低峰执行灌数。</p>
 */
public interface BatchExecutor {

    /** 推荐批次大小（MySQL 500；PG COPY 单批可更大） */
    int getBatchSize();

    /**
     * 执行批量插入。rows 中每个 Object[] 为一行参数（顺序与 sql 占位符一致），
     * 元素类型已由引擎转换为 JDBC 友好类型（Integer/Long/BigDecimal/Timestamp/Boolean/String/null）。
     *
     * @return 实际插入行数（试运行时调用方不应调用本方法）
     */
    int executeBatch(Connection conn, String sql, List<Object[]> rows) throws SQLException;

    /** 是否支持事务内分段提交（防止长事务；每表独立事务由引擎编排） */
    default boolean supportChunkedTransaction() {
        return true;
    }
}
