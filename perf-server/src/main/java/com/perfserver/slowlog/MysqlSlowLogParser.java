package com.perfserver.slowlog;

import org.springframework.stereotype.Component;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * MySQL 慢查询日志解析器（慢 SQL 采集第二通道）。
 *
 * <p>解析标准 MySQL 慢日志格式：
 * <pre>
 * # Time: 2026-08-27T13:00:00.123456Z
 * # User@Host: root[root] @ localhost [127.0.0.1]
 * # Query_time: 1.283376  Lock_time: 0.000145  Rows_sent: 10  Rows_examined: 1000
 * SELECT * FROM t_user WHERE id = 1;
 * </pre>
 */
@Component
public class MysqlSlowLogParser {

    private static final Pattern TIME_PATTERN = Pattern.compile("^# Time: (.*)$");
    private static final Pattern USER_HOST_PATTERN = Pattern.compile("^# User@Host: ([^\\[]+)\\[[^\\]]*\\] @ ([^\\[]+)\\s*\\[?([^\\]]*)\\]?");
    private static final Pattern QUERY_TIME_PATTERN = Pattern.compile(
            "^# Query_time: ([\\d.]+)\\s+Lock_time: ([\\d.]+)\\s+Rows_sent: (\\d+)\\s+Rows_examined: (\\d+)");
    private static final Pattern QUERY_TIME_SHORT_PATTERN = Pattern.compile("^# Query_time: ([\\d.]+)");

    private static final DateTimeFormatter ISO_TIME = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss");

    /** 解析日志文本，返回慢 SQL 条目列表 */
    public List<Entry> parse(String content) {
        List<Entry> entries = new ArrayList<>();
        if (content == null || content.isBlank()) {
            return entries;
        }
        String[] lines = content.split("\\r?\\n");
        Entry current = null;
        StringBuilder sql = new StringBuilder();

        for (String line : lines) {
            if (line.startsWith("# Time:")) {
                // 结束上一个条目
                finishEntry(entries, current, sql);
                current = new Entry();
                sql.setLength(0);
                Matcher m = TIME_PATTERN.matcher(line);
                if (m.find()) {
                    current.executeTime = parseTime(m.group(1).trim());
                }
            } else if (line.startsWith("# User@Host:") && current != null) {
                Matcher m = USER_HOST_PATTERN.matcher(line);
                if (m.find()) {
                    current.user = m.group(1).trim();
                    current.host = m.group(2).trim();
                }
            } else if (line.startsWith("# Query_time:") && current != null) {
                Matcher m = QUERY_TIME_PATTERN.matcher(line);
                if (m.find()) {
                    current.queryTimeMs = parseMs(m.group(1));
                    current.lockTimeMs = parseMs(m.group(2));
                    current.rowsSent = Long.parseLong(m.group(3));
                    current.rowsExamined = Long.parseLong(m.group(4));
                } else {
                    Matcher shortM = QUERY_TIME_SHORT_PATTERN.matcher(line);
                    if (shortM.find()) {
                        current.queryTimeMs = parseMs(shortM.group(1));
                    }
                }
            } else if (line.startsWith("#")) {
                // 其他注释行（# admin@ 等）忽略
            } else if (!line.isBlank() && current != null) {
                // SQL 语句行（可能多行，直到下一个 # Time）
                if (sql.length() > 0) {
                    sql.append(' ');
                }
                sql.append(line.trim());
            }
        }
        finishEntry(entries, current, sql);
        return entries;
    }

    private void finishEntry(List<Entry> entries, Entry entry, StringBuilder sql) {
        if (entry == null || entry.queryTimeMs <= 0) {
            return;
        }
        String sqlText = sql.toString().trim();
        if (sqlText.isEmpty()) {
            return;
        }
        entry.sqlText = sqlText;
        entries.add(entry);
    }

    /** 解析 MySQL 时间格式（支持带 T 的 ISO 与带空格的本地格式） */
    private LocalDateTime parseTime(String raw) {
        String v = raw.trim();
        // 去掉末尾时区后缀与毫秒
        int dot = v.indexOf('.');
        if (dot > 0) {
            v = v.substring(0, dot);
        }
        v = v.replace('T', ' ').trim();
        try {
            return LocalDateTime.parse(v, DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"));
        } catch (Exception e) {
            // 兜底
            return LocalDateTime.now();
        }
    }

    private double parseMs(String raw) {
        try {
            return Double.parseDouble(raw.trim()) * 1000.0;
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    /** 慢日志条目 */
    public static class Entry {
        private LocalDateTime executeTime;
        private String user;
        private String host;
        private double queryTimeMs;
        private double lockTimeMs;
        private long rowsSent;
        private long rowsExamined;
        private String sqlText;

        public LocalDateTime getExecuteTime() {
            return executeTime;
        }

        public void setExecuteTime(LocalDateTime executeTime) {
            this.executeTime = executeTime;
        }

        public String getUser() {
            return user;
        }

        public void setUser(String user) {
            this.user = user;
        }

        public String getHost() {
            return host;
        }

        public void setHost(String host) {
            this.host = host;
        }

        public double getQueryTimeMs() {
            return queryTimeMs;
        }

        public void setQueryTimeMs(double queryTimeMs) {
            this.queryTimeMs = queryTimeMs;
        }

        public double getLockTimeMs() {
            return lockTimeMs;
        }

        public void setLockTimeMs(double lockTimeMs) {
            this.lockTimeMs = lockTimeMs;
        }

        public long getRowsSent() {
            return rowsSent;
        }

        public void setRowsSent(long rowsSent) {
            this.rowsSent = rowsSent;
        }

        public long getRowsExamined() {
            return rowsExamined;
        }

        public void setRowsExamined(long rowsExamined) {
            this.rowsExamined = rowsExamined;
        }

        public String getSqlText() {
            return sqlText;
        }

        public void setSqlText(String sqlText) {
            this.sqlText = sqlText;
        }
    }
}
