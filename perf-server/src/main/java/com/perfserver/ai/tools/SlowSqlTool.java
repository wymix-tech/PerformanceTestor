package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.perfserver.entity.SlowSql;
import com.perfserver.service.MetricQueryService;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

/**
 * 慢 SQL 查询工具：返回最近时间窗口内的慢 SQL 列表（SQL 文本/耗时/来源）。
 */
@Component
public class SlowSqlTool extends AbstractAiTool {

    private final MetricQueryService metricQueryService;

    public SlowSqlTool(MetricQueryService metricQueryService) {
        this.metricQueryService = metricQueryService;
    }

    @Override
    public String name() {
        return "slow_sql";
    }

    @Override
    public String description() {
        return "查询最近慢 SQL。参数：app 应用名（可选）；minutes 最近多少分钟（默认 1440，即一天）；"
                + "minCostMs 最小耗时过滤（默认不限制）；limit 条数上限（默认 50）。"
                + "返回 SQL 文本、耗时毫秒、来源（jdbc/slowlog）、执行时间。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"app", "minutes", "minCostMs", "limit"}, null).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        String app = str(args, "app", null);
        int minutes = integer(args, "minutes", 1440);
        Double minCost = args.path("minCostMs").isMissingNode() || args.path("minCostMs").isNull()
                ? null : args.path("minCostMs").asDouble();
        int limit = integer(args, "limit", 50);
        try {
            LocalDateTime end = LocalDateTime.now();
            List<SlowSql> sqls = metricQueryService.slowSqls(app, end.minusMinutes(minutes), end, minCost, limit);
            ArrayNode list = mapper.createArrayNode();
            for (SlowSql s : sqls) {
                var n = list.addObject();
                n.put("id", s.getId());
                n.put("appName", s.getAppName());
                n.put("sqlText", s.getSqlText());
                n.put("costMs", s.getCostMs());
                n.put("source", s.getSource());
                n.put("dbHost", s.getDbHost());
                n.put("executeTime", s.getExecuteTime() == null ? "" : s.getExecuteTime().toString());
            }
            return ok(Map.of("count", sqls.size(), "slowSqls", list));
        } catch (Exception e) {
            return error("慢 SQL 查询失败: " + e.getMessage());
        }
    }
}
