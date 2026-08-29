package com.perftestor.datagen.executor;

import com.perftestor.datagen.spi.BatchExecutor;

import java.math.BigDecimal;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.LocalDateTime;
import java.util.List;

/**
 * JDBC Batch 执行器（承接原 DataGenService 批量插入逻辑：addBatch/executeBatch，
 * 元素已由引擎转换为 JDBC 友好类型）。
 *
 * <p><b>性能警告</b>：热点倾斜极高（refSkew 使单热行承载 &gt;50% 子行）时，高并发压测中
 * 数据库行锁冲突会抵消批量写入收益。建议调用方降低 BATCH_SIZE（如 500→200）或为子表分区。</p>
 */
public class JdbcBatchExecutor implements BatchExecutor {

    public static final int DEFAULT_BATCH_SIZE = 500;

    private final int batchSize;

    public JdbcBatchExecutor() {
        this(DEFAULT_BATCH_SIZE);
    }

    public JdbcBatchExecutor(int batchSize) {
        this.batchSize = Math.max(1, batchSize);
    }

    @Override
    public int getBatchSize() {
        return batchSize;
    }

    @Override
    public int executeBatch(Connection conn, String sql, List<Object[]> rows) throws SQLException {
        if (rows.isEmpty()) {
            return 0;
        }
        int inserted = 0;
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            for (Object[] row : rows) {
                for (int i = 0; i < row.length; i++) {
                    setObject(ps, i + 1, row[i]);
                }
                ps.addBatch();
            }
            int[] results = ps.executeBatch();
            for (int r : results) {
                inserted += (r == java.sql.Statement.SUCCESS_NO_INFO || r >= 0) ? 1 : 0;
                // SUCCESS_NO_INFO(-2) 在 rewriteBatchedStatements 下常见，按 1 行计
                if (r == java.sql.Statement.SUCCESS_NO_INFO) {
                    // 单语句多行改写时以行数为准由调用方统计，这里保守累加 1
                }
            }
            // rewriteBatchedStatements 场景 executeBatch 不返回精确行数，
            // 调用方（引擎）以 rows.size() 作为权威插入计数。
            inserted = rows.size();
        }
        return inserted;
    }

    /** 按值运行时类型设置参数（引擎侧已完成类型转换，此处兜底 LocalDateTime 等） */
    static void setObject(PreparedStatement ps, int idx, Object v) throws SQLException {
        if (v == null) {
            ps.setObject(idx, null);
            return;
        }
        if (v instanceof LocalDateTime) {
            ps.setTimestamp(idx, Timestamp.valueOf((LocalDateTime) v));
        } else if (v instanceof java.time.LocalDate) {
            ps.setDate(idx, java.sql.Date.valueOf((java.time.LocalDate) v));
        } else if (v instanceof BigDecimal || v instanceof Long || v instanceof Integer
                || v instanceof Double || v instanceof Float || v instanceof Boolean
                || v instanceof String || v instanceof Timestamp || v instanceof Short
                || v instanceof Byte) {
            ps.setObject(idx, v);
        } else {
            ps.setString(idx, String.valueOf(v));
        }
    }
}
