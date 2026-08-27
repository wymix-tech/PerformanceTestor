package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.SlowSql;
import com.perfserver.mapper.SlowSqlMapper;
import com.perfserver.slowlog.MysqlSlowLogParser;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 慢查询日志导入 API：粘贴/上传 MySQL 慢日志文本，解析后以 slowlog 通道入库。
 */
@RestController
@RequestMapping("/api/slowlog")
public class SlowLogController {

    private static final Logger log = LoggerFactory.getLogger(SlowLogController.class);

    private final MysqlSlowLogParser parser;
    private final SlowSqlMapper slowSqlMapper;

    public SlowLogController(MysqlSlowLogParser parser, SlowSqlMapper slowSqlMapper) {
        this.parser = parser;
        this.slowSqlMapper = slowSqlMapper;
    }

    /** 解析并入库（body：{appName, content, dbHost?}） */
    @PostMapping("/parse")
    public ApiResponse<Map<String, Object>> parse(@RequestBody Map<String, Object> body) {
        String appName = body.get("appName") == null ? "unknown" : String.valueOf(body.get("appName"));
        String content = body.get("content") == null ? "" : String.valueOf(body.get("content"));
        String dbHost = body.get("dbHost") == null ? "" : String.valueOf(body.get("dbHost"));
        if (content.length() > 5 * 1024 * 1024) {
            return ApiResponse.error("慢日志文本超过 5MB 上限");
        }
        List<MysqlSlowLogParser.Entry> entries = parser.parse(content);
        List<SlowSql> batch = new ArrayList<>(entries.size());
        for (MysqlSlowLogParser.Entry entry : entries) {
            SlowSql s = new SlowSql();
            s.setAppName(appName);
            s.setSqlText(entry.getSqlText());
            s.setCostMs(entry.getQueryTimeMs());
            s.setSource("slowlog");
            s.setDbHost(dbHost.isBlank() ? entry.getHost() : dbHost);
            s.setExecuteTime(entry.getExecuteTime());
            batch.add(s);
        }
        if (!batch.isEmpty()) {
            slowSqlMapper.batchInsert(batch);
        }
        log.info("slowlog parsed: app={}, entries={}", appName, entries.size());
        return ApiResponse.ok(Map.of("parsed", entries.size()));
    }
}
