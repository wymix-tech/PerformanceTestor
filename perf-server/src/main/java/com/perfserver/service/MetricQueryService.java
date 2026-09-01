package com.perfserver.service;

import com.perfserver.entity.AppInstance;
import com.perfserver.entity.JvmMetric;
import com.perfserver.entity.SlowCommand;
import com.perfserver.entity.SlowSql;
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
