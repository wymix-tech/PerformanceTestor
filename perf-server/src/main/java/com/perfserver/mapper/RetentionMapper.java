package com.perfserver.mapper;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

/**
 * 数据保留策略 Mapper：分区维护（新建 / 删除日分区）与普通表分批清理。
 *
 * <p>分区表（{@code jvm_metric} / {@code stress_sample}）由 {@code information_schema.PARTITIONS}
 * 回查现有分区名；分区 DDL 无法用占位符绑定表名/分区名（MySQL DDL 不支持参数化标识符），
 * 故表名与分区名在 Java 侧做白名单校验后由 Mapper 侧白名单常量拼接，禁止外部字符串直入。
 */
@Mapper
public interface RetentionMapper {

    /**
     * 查询指定表的现有分区（分区名 + 上界）。
     *
     * @param tableName 表名（Java 侧已白名单校验）
     * @return 每项含 {@code PARTITION_NAME} / {@code PARTITION_DESCRIPTION}
     */
    List<Map<String, Object>> listPartitions(@Param("tableName") String tableName);

    /**
     * 新增一个日分区（DDL，非参数化）。
     *
     * @param tableName 表名（白名单）
     * @param partition 分区名（形如 {@code p20260924}，Java 侧正则校验）
     * @param lessThan  分区上界（不含），如 {@code 2026-09-25 00:00:00}
     */
    void addDailyPartition(@Param("tableName") String tableName,
                           @Param("partition") String partition,
                           @Param("lessThan") String lessThan);

    /**
     * 删除一个分区（DDL，非参数化）。DROP PARTITION 同时删除分区内全部数据。
     *
     * @param tableName 表名（白名单）
     * @param partition 分区名（Java 侧正则校验）
     */
    void dropPartition(@Param("tableName") String tableName, @Param("partition") String partition);

    /** 重建 p_max 分区（DROP + ADD），防止 REORGANIZE 在部分版本上的兼容性问题 */
    void reorganizeMaxPartition(@Param("tableName") String tableName,
                               @Param("partition") String partition,
                               @Param("lessThan") String lessThan);

    /**
     * 分批删除早于阈值的数据（普通表；部分分区表也适用，走索引可剪枝到分区）。
     *
     * @return 本次删除行数（&lt; batchSize 表示已删完）
     */
    int deleteBefore(@Param("tableName") String tableName,
                     @Param("column") String column,
                     @Param("before") LocalDateTime before,
                     @Param("batchSize") int batchSize);
}
