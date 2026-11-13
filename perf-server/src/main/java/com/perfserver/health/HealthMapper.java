package com.perfserver.health;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 健康检查与启动自检的数据回查 Mapper。
 *
 * <p>只暴露元数据查询（{@code information_schema} / 连接探活），不做任何写操作：
 * 自检与健康检查会以较高频率运行，必须保证幂等且无副作用。
 */
@Mapper
public interface HealthMapper {

    /** 库连通性探活（简单 SELECT 1） */
    Integer ping();

    /** 数据库产品与版本（{@code MySQL 8.0.36} 形式），连接异常时上层捕获为不可用 */
    String databaseVersion();

    /**
     * 查询已存在的表名（仅返回白名单内的表）。
     *
     * @param tables 待检查的表名清单
     * @return 实际存在的表名
     */
    List<String> findExistingTables(@Param("tables") List<String> tables);

    /**
     * 查询按天分区的分区表实际分区数（含 {@code p_max}）。
     *
     * @return 表名 → 分区数，仅返回已分区表
     */
    List<String> findPartitionedTables(@Param("tables") List<String> tables);
}
