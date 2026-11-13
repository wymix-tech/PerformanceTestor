package com.perfserver.mapper;

import com.perfserver.entity.SlowSql;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 慢 SQL Mapper：批量写入 + 时间窗口/耗时阈值查询。
 */
@Mapper
public interface SlowSqlMapper {

    /** 批量插入（JDBC 拦截与慢日志解析均可走此通道） */
    int batchInsert(List<SlowSql> list);

    /** 查询：应用/时间窗口/最小耗时过滤，按耗时倒序 */
    List<SlowSql> query(@Param("appName") String appName,
                        @Param("start") LocalDateTime start,
                        @Param("end") LocalDateTime end,
                        @Param("minCostMs") Double minCostMs,
                        @Param("limit") int limit);

    /**
     * 游标分页查询：按 {@code (execute_time, id)} 复合游标倒序翻页。
     *
     * <p>排序键用 {@code execute_time} 而非热点排名的 {@code cost_ms}：
     * 游标翻页要求排序键在翻页期间不会因新数据插入而重排，耗时可被后到的慢 SQL 反超，
     * 用它做游标会漏读/重读。按执行时间倒序是唯一稳定且符合"看最新"直觉的次序。
     */
    List<SlowSql> queryByCursor(@Param("appName") String appName,
                                @Param("start") LocalDateTime start,
                                @Param("end") LocalDateTime end,
                                @Param("minCostMs") Double minCostMs,
                                @Param("cursorTime") LocalDateTime cursorTime,
                                @Param("cursorId") Long cursorId,
                                @Param("limit") int limit);

    /** 按主键查询（慢 SQL 分析时取 SQL 文本与方法链） */
    SlowSql findById(@Param("id") long id);
}
