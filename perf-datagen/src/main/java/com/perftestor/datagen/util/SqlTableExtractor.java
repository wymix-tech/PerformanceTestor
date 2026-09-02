package com.perftestor.datagen.util;

import com.perftestor.datagen.model.EndpointTableFact;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * SQL 表名与读写类型抽取器。
 *
 * <p>原则「宁缺勿滥」：只接受确定可判定的形态（单表 {@code FROM t} / {@code JOIN t} /
 * {@code UPDATE t} / {@code INSERT INTO t}，可带反引号与库名前缀）。遇到子查询、CTE、
 * 多表 UPDATE 等不完整形态时<b>不猜</b>：已确定的表照常给出，同时在
 * {@link Result#getWarning()} 写入原因，由前端黄色提示引导手工补表。</p>
 *
 * <p>理由：对错误的表造数据（可能污染业务表）危害远大于漏表，而漏表有明确的告警出口。</p>
 *
 * <p>任何异常静默返回空结果，绝不抛出中断分析。</p>
 */
public final class SqlTableExtractor {

    private SqlTableExtractor() {
    }

    /** 表名候选（含可选库名前缀与反引号） */
    private static final String TBL = "([`\\w.]+)";

    private static final Pattern FROM = Pattern.compile("\\bfrom\\s+" + TBL, Pattern.CASE_INSENSITIVE);
    private static final Pattern JOIN = Pattern.compile("\\bjoin\\s+" + TBL, Pattern.CASE_INSENSITIVE);
    private static final Pattern INTO = Pattern.compile("\\binto\\s+" + TBL, Pattern.CASE_INSENSITIVE);
    private static final Pattern UPDATE = Pattern.compile("\\bupdate\\s+" + TBL, Pattern.CASE_INSENSITIVE);

    private static final Pattern LEADING_VERB = Pattern.compile("^\\s*(select|insert|update|delete|replace)\\b",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern LEADING_DELETE_FROM = Pattern.compile("^\\s*delete\\s+from\\b",
            Pattern.CASE_INSENSITIVE);
    /** 多表 UPDATE：update a, b set ...（表可带别名） */
    private static final Pattern MULTI_TABLE_UPDATE =
            Pattern.compile("\\bupdate\\s+[`\\w.]+(?:\\s+(?:as\\s+)?\\w+)?\\s*,", Pattern.CASE_INSENSITIVE);
    private static final Pattern SUBQUERY = Pattern.compile("\\b(?:from|join)\\s*\\(", Pattern.CASE_INSENSITIVE);
    private static final Pattern CTE = Pattern.compile("\\bwith\\s+[\\w`]+\\s+as\\s*\\(", Pattern.CASE_INSENSITIVE);
    private static final Pattern LIMIT = Pattern.compile("\\blimit\\s+(\\d+)", Pattern.CASE_INSENSITIVE);

    /** 合法表名（小写后校验），用于剔除误吸的 SQL 关键字与符号片段 */
    private static final Pattern VALID_TABLE = Pattern.compile("[a-z_][a-z0-9_]{0,63}");

    /**
     * 误吸关键字黑名单。
     *
     * <p>注意：表名 {@code order} 是业务常见表名，<b>不入黑名单</b>——
     * 它只可能出现在 from/join/update/into 之后，此时它就是表名而非 ORDER BY 子句。</p>
     */
    private static final Set<String> SQL_KEYWORDS = Set.of(
            "select", "from", "where", "and", "or", "not", "null", "set", "values", "value", "on", "using",
            "having", "group", "limit", "offset", "union", "all", "distinct", "as", "join", "inner",
            "left", "right", "outer", "cross", "into", "update", "delete", "insert", "case", "when", "then",
            "else", "end", "like", "between", "in", "exists", "asc", "desc", "by", "with", "recursive",
            "for", "duplicate", "key", "ignore", "straight_join", "natural", "force", "use", "index");

    /** 抽取结果 */
    public static final class Result {
        private final List<TableAccess> tables = new ArrayList<>();
        /** 非空表示结论可能不完整（仅提示，不阻断） */
        private String warning;
        /** SQL 中的 limit 字面量（0 表示未识别） */
        private int pageSize;

        public List<TableAccess> getTables() {
            return tables;
        }

        public String getWarning() {
            return warning;
        }

        public int getPageSize() {
            return pageSize;
        }

        /** 表名集合（去重，保持出现顺序） */
        public List<String> tableNames() {
            Set<String> seen = new LinkedHashSet<>();
            for (TableAccess t : tables) {
                seen.add(t.getTable());
            }
            return new ArrayList<>(seen);
        }
    }

    /** 单表访问：表名 + 读写类型 */
    public static final class TableAccess {
        private final String table;
        private final EndpointTableFact.SqlAccess access;

        TableAccess(String table, EndpointTableFact.SqlAccess access) {
            this.table = table;
            this.access = access;
        }

        public String getTable() {
            return table;
        }

        public EndpointTableFact.SqlAccess getAccess() {
            return access;
        }

        @Override
        public String toString() {
            return table + "/" + access;
        }
    }

    /**
     * 抽取 SQL 中的表名与读写类型。
     *
     * @param sql SQL 文本（可为动态标签拼接后的碎片）
     * @return 抽取结果（不抛异常；无命中时 tables 为空）
     */
    public static Result extract(String sql) {
        Result result = new Result();
        if (sql == null || sql.isBlank()) {
            return result;
        }
        try {
            String text = sql;
            Matcher verb = LEADING_VERB.matcher(text);
            String leading = verb.find() ? verb.group(1).toLowerCase(Locale.ROOT) : "";
            boolean deleteFrom = LEADING_DELETE_FROM.matcher(text).find();

            Matcher lim = LIMIT.matcher(text);
            if (lim.find()) {
                try {
                    result.pageSize = Integer.parseInt(lim.group(1));
                } catch (NumberFormatException ignore) {
                    // 超大字面量：按未识别处理
                }
            }

            // INTO / UPDATE 目标恒为写；DELETE FROM 的 from 目标为写；其余 from/join 为读
            collect(text, INTO, EndpointTableFact.SqlAccess.WRITE, result);
            collect(text, UPDATE, EndpointTableFact.SqlAccess.WRITE, result);
            collect(text, FROM, deleteFrom ? EndpointTableFact.SqlAccess.WRITE : EndpointTableFact.SqlAccess.READ,
                    result);
            collect(text, JOIN, EndpointTableFact.SqlAccess.READ, result);

            // 写语句中经 FROM 读到的表仍可能是真实读取（INSERT ... SELECT），此处不做改写
            if ("insert".equals(leading) || "update".equals(leading) || "delete".equals(leading)
                    || "replace".equals(leading)) {
                // 写语句里若没有任何 WRITE 目标（如动态 SQL 缺 into），降级提示
                boolean hasWrite = result.getTables().stream()
                        .anyMatch(t -> t.getAccess() == EndpointTableFact.SqlAccess.WRITE);
                if (!hasWrite) {
                    result.warning = "写语句未识别到写入目标表（动态 SQL 碎片？）";
                }
            }

            if (SUBQUERY.matcher(text).find()) {
                result.warning = merge(result.warning, "含子查询/派生表，涉及表可能不完整");
            }
            if (CTE.matcher(text).find()) {
                result.warning = merge(result.warning, "含 CTE（WITH ... AS），涉及表可能不完整");
            }
            if (MULTI_TABLE_UPDATE.matcher(text).find()) {
                result.warning = merge(result.warning, "多表 UPDATE 未完整解析，仅取首个表");
            }
        } catch (Exception ignore) {
            // 容错：静默
        }
        return result;
    }

    private static void collect(String sql, Pattern p, EndpointTableFact.SqlAccess access, Result out) {
        Matcher m = p.matcher(sql);
        while (m.find()) {
            String table = sanitize(m.group(1));
            if (table == null) {
                continue;
            }
            if (out.getTables().stream().anyMatch(t -> t.getTable().equals(table))) {
                continue;
            }
            out.getTables().add(new TableAccess(table, access));
        }
    }

    /** 是否为 SQL 关键字（供同类抽取器复用，避免把关键字误判为列名/别名） */
    static boolean isKeywordCandidate(String s) {
        if (s == null) {
            return true;
        }
        String v = s.replace("`", "").trim().toLowerCase(Locale.ROOT);
        return v.isEmpty() || SQL_KEYWORDS.contains(v);
    }

    /** 表名清洗：去反引号、去库名前缀、小写、剔除关键字与非法名 */
    static String sanitize(String raw) {
        if (raw == null) {
            return null;
        }
        String s = raw.replace("`", "").trim();
        int dot = s.lastIndexOf('.');
        if (dot >= 0) {
            s = s.substring(dot + 1);
        }
        s = s.toLowerCase(Locale.ROOT);
        if (s.isEmpty() || SQL_KEYWORDS.contains(s) || !VALID_TABLE.matcher(s).matches()) {
            return null;
        }
        return s;
    }

    private static String merge(String a, String b) {
        if (a == null || a.isBlank()) {
            return b;
        }
        return a + "；" + b;
    }
}
