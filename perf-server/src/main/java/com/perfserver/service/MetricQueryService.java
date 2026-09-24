package com.perfserver.service;

import com.perfserver.entity.AppInstance;
import com.perfserver.entity.JvmMetric;
import com.perfserver.entity.SlowCommand;
import com.perfserver.entity.SlowSql;
import com.perfserver.common.page.CursorCodec;
import com.perfserver.common.page.CursorPage;
import com.perfserver.common.page.CursorPageRequest;
import com.perfserver.entity.TraceChain;
import com.perfserver.mapper.AppInstanceMapper;
import com.perfserver.mapper.JvmMetricMapper;
import com.perfserver.mapper.SlowCommandMapper;
import com.perfserver.mapper.SlowSqlMapper;
import com.perfserver.mapper.TraceChainMapper;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeParseException;
import java.util.List;

/**
 * 采集数据查询服务：JVM 指标 / 调用链 / 慢 SQL / 应用实例的统一查询入口。
 *
 * <p>供 Web 查询 API 与后续容量分析、AI 工具复用，避免各模块直接依赖 Mapper。
 */
@Service
public class MetricQueryService {

    private final AppInstanceMapper appInstanceMapper;
    private final JvmMetricMapper jvmMetricMapper;
    private final TraceChainMapper traceChainMapper;
    private final SlowSqlMapper slowSqlMapper;
    private final SlowCommandMapper slowCommandMapper;

    public MetricQueryService(AppInstanceMapper appInstanceMapper,
                              JvmMetricMapper jvmMetricMapper,
                              TraceChainMapper traceChainMapper,
                              SlowSqlMapper slowSqlMapper,
                              SlowCommandMapper slowCommandMapper) {
        this.appInstanceMapper = appInstanceMapper;
        this.jvmMetricMapper = jvmMetricMapper;
        this.traceChainMapper = traceChainMapper;
        this.slowSqlMapper = slowSqlMapper;
        this.slowCommandMapper = slowCommandMapper;
    }

    /** 全部应用实例（在线优先） */
    public List<AppInstance> listApps() {
        return appInstanceMapper.findAll();
    }

    /** JVM 指标：时间窗口查询（默认返回 app 最近 N 条；start/end 为空时回退最近 1 小时） */
    public List<JvmMetric> jvmMetrics(String app, LocalDateTime start, LocalDateTime end, int limit) {
        if (start == null) {
            start = LocalDateTime.now().minusHours(1);
        }
        if (end == null) {
            end = LocalDateTime.now();
        }
        return jvmMetricMapper.query(app, start, end, Math.max(1, Math.min(limit, 5000)));
    }

    /** 调用链列表：应用/根方法关键字/时间窗口 */
    public List<TraceChain> traces(String app, String method, LocalDateTime start, LocalDateTime end, int limit) {
        if (start == null) {
            start = LocalDateTime.now().minusHours(1);
        }
        if (end == null) {
            end = LocalDateTime.now();
        }
        return traceChainMapper.query(app, method, start, end, Math.max(1, Math.min(limit, 500)));
    }

    /** 调用链详情 */
    public TraceChain traceDetail(String traceId) {
        return traceChainMapper.findByTraceId(traceId);
    }

    /** 慢 SQL：应用/时间窗口/最小耗时 */
    public List<SlowSql> slowSqls(String app, LocalDateTime start, LocalDateTime end, Double minCostMs, int limit) {
        if (start == null) {
            start = LocalDateTime.now().minusHours(1);
        }
        if (end == null) {
            end = LocalDateTime.now();
        }
        return slowSqlMapper.query(app, start, end, minCostMs, Math.max(1, Math.min(limit, 500)));
    }

    /**
     * 中间件慢命令：应用/类型/时间窗口/最小耗时。
     *
     * @param type redis/es/mq，空表示全部
     */
    public List<SlowCommand> slowCommands(String app, String type, LocalDateTime start, LocalDateTime end,
                                          Double minCostMs, int limit) {
        if (start == null) {
            start = LocalDateTime.now().minusHours(1);
        }
        if (end == null) {
            end = LocalDateTime.now();
        }
        return slowCommandMapper.query(app, type, start, end, minCostMs, Math.max(1, Math.min(limit, 500)));
    }

    // ==================== 游标分页（P1-2）====================

    /**
     * 时序列表统一游标分页入口。
     *
     * <p>多取一条（{@code size + 1}）判断是否还有下一页：这样"最后一页"与"正好取满"
     * 能区分开，否则前端只能在空页时才停手，会多发一次必空的请求。
     * 返回时把多取的那条丢掉，游标取实际返回的最后一条。
     */
    public <T> CursorPage<T> page(int size, String cursor, EntityCursorQuery<T> query, CursorKeyAccessor<T> key) {
        int normalized = CursorPageRequest.normalizeSize(size);
        CursorCodec.Cursor decoded = CursorCodec.decode(cursor);
        List<T> rows = query.fetch(decoded, normalized + 1);
        if (rows == null || rows.isEmpty()) {
            return CursorPage.empty();
        }
        boolean hasMore = rows.size() > normalized;
        List<T> items = hasMore ? rows.subList(0, normalized) : rows;
        String nextCursor = hasMore ? encodeCursor(key, items.get(items.size() - 1)) : null;
        return new CursorPage<>(items, nextCursor, hasMore);
    }

    private static <T> String encodeCursor(CursorKeyAccessor<T> key, T last) {
        LocalDateTime time = key.timeOf(last);
        long id = key.idOf(last);
        return CursorCodec.encode(time, id);
    }

    /** 单页取数回调：{@code cursor} 为 null 表示首屏 */
    @FunctionalInterface
    public interface EntityCursorQuery<T> {
        List<T> fetch(CursorCodec.Cursor cursor, int limit);
    }

    /** 排序键访问器：从实体取 {@code (时间, id)} 复合键 */
    @FunctionalInterface
    public interface CursorKeyAccessor<T> {
        LocalDateTime timeOf(T entity);

        default long idOf(T entity) {
            return 0L;
        }
    }

    /** JVM 指标游标分页（最新在前） */
    public CursorPage<JvmMetric> jvmMetricsPage(String app, LocalDateTime start, LocalDateTime end,
                                                int size, String cursor) {
        LocalDateTime s = start == null ? LocalDateTime.now().minusHours(1) : start;
        LocalDateTime e = end == null ? LocalDateTime.now() : end;
        return page(size, cursor,
                (c, limit) -> jvmMetricMapper.queryByCursor(app, s, e,
                        c == null ? null : c.time(), c == null ? null : c.id(), limit),
                new CursorKeyAccessor<>() {
                    @Override
                    public LocalDateTime timeOf(JvmMetric m) {
                        return m.getCollectTime();
                    }

                    @Override
                    public long idOf(JvmMetric m) {
                        return m.getId() == null ? 0L : m.getId();
                    }
                });
    }

    /**
     * 调用链游标分页（最新在前）。
     *
     * <p>{@code limit}/{@code size} 兼容：显式传 {@code size} 用 size，
     * 仅传旧 {@code limit} 时沿用 limit，保证存量前端不改也能翻页。
     */
    public CursorPage<TraceChain> tracesPage(String app, String method, LocalDateTime start, LocalDateTime end,
                                             int size, Integer legacyLimit, String cursor) {
        int normalized = CursorPageRequest.normalizeSize(size, legacyLimit);
        LocalDateTime s = start == null ? LocalDateTime.now().minusHours(1) : start;
        LocalDateTime e = end == null ? LocalDateTime.now() : end;
        return page(normalized, cursor,
                (c, limit) -> traceChainMapper.queryByCursor(app, method, s, e,
                        c == null ? null : c.time(), c == null ? null : c.id(), limit),
                new CursorKeyAccessor<>() {
                    @Override
                    public LocalDateTime timeOf(TraceChain t) {
                        return t.getStartTime();
                    }

                    @Override
                    public long idOf(TraceChain t) {
                        return t.getId() == null ? 0L : t.getId();
                    }
                });
    }

    /** 慢 SQL 游标分页（按执行时间倒序） */
    public CursorPage<SlowSql> slowSqlsPage(String app, LocalDateTime start, LocalDateTime end, Double minCostMs,
                                            int size, Integer legacyLimit, String cursor) {
        int normalized = CursorPageRequest.normalizeSize(size, legacyLimit);
        LocalDateTime s = start == null ? LocalDateTime.now().minusHours(1) : start;
        LocalDateTime e = end == null ? LocalDateTime.now() : end;
        return page(normalized, cursor,
                (c, limit) -> slowSqlMapper.queryByCursor(app, s, e, minCostMs,
                        c == null ? null : c.time(), c == null ? null : c.id(), limit),
                new CursorKeyAccessor<>() {
                    @Override
                    public LocalDateTime timeOf(SlowSql x) {
                        return x.getExecuteTime();
                    }

                    @Override
                    public long idOf(SlowSql x) {
                        return x.getId() == null ? 0L : x.getId();
                    }
                });
    }

    /** 中间件慢命令游标分页（按执行时间倒序） */
    public CursorPage<SlowCommand> slowCommandsPage(String app, String type, LocalDateTime start, LocalDateTime end,
                                                    Double minCostMs, int size, Integer legacyLimit, String cursor) {
        int normalized = CursorPageRequest.normalizeSize(size, legacyLimit);
        LocalDateTime s = start == null ? LocalDateTime.now().minusHours(1) : start;
        LocalDateTime e = end == null ? LocalDateTime.now() : end;
        return page(normalized, cursor,
                (c, limit) -> slowCommandMapper.queryByCursor(app, type, s, e, minCostMs,
                        c == null ? null : c.time(), c == null ? null : c.id(), limit),
                new CursorKeyAccessor<>() {
                    @Override
                    public LocalDateTime timeOf(SlowCommand x) {
                        return x.getExecuteTime();
                    }

                    @Override
                    public long idOf(SlowCommand x) {
                        return x.getId() == null ? 0L : x.getId();
                    }
                });
    }

    /**
     * 时间参数解析：支持 epoch 毫秒数字或 ISO-8601 字符串，无法解析返回默认值。
     */
    public static LocalDateTime parseTime(String value, LocalDateTime defaultValue) {
        if (value == null || value.isBlank()) {
            return defaultValue;
        }
        String v = value.trim();
        try {
            return Instant.ofEpochMilli(Long.parseLong(v)).atZone(ZoneId.systemDefault()).toLocalDateTime();
        } catch (NumberFormatException ignored) {
            // 非纯数字，尝试 ISO 时间字符串
        }
        try {
            return LocalDateTime.parse(v);
        } catch (DateTimeParseException ignored) {
            // 尝试仅日期（yyyy-MM-dd）
        }
        try {
            return java.time.LocalDate.parse(v).atStartOfDay();
        } catch (DateTimeParseException ignored) {
            return defaultValue;
        }
    }
}
