package com.perftestor.datagen.util;

import com.perftestor.datagen.model.EndpointTableFact;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * SQL WHERE 谓词抽取器：等值 / IN 集合 / 范围（含 BETWEEN）。
 *
 * <p>产出直接驱动「数据如何构成」——等值谓词转枚举值集、范围谓词转区间分布。
 * 只有带<b>字面量</b>的谓词才有数据构造价值；{@code status = #{status}} 这类
 * 占位符谓词天然不被正则命中，无需特殊处理。</p>
 *
 * <p>列归属：优先认表限定谓词（{@code o.status = 1}，别名须属于目标表）；
 * 当 SQL 中不存在任何表别名时，才额外接受非限定谓词（{@code status = 1}）。</p>
 *
 * <p>含 WHERE 但抽不任何谓词（多为 {@code col = #{param}} 占位符形态）时返回空列表——
 * 占位符无数据构造价值，由调用方按「无信号」处理。</p>
 *
 * <p>任何异常静默返回，绝不抛出中断分析。</p>
 */
public final class SqlPredicateExtractor {

    private SqlPredicateExtractor() {
    }

    private static final Pattern EQ_STR = Pattern.compile("([\\w]+)\\.([\\w`]+)\\s*=\\s*'([^']*)'");
    private static final Pattern EQ_NUM = Pattern.compile("([\\w]+)\\.([\\w`]+)\\s*=\\s*(\\d+)");
    private static final Pattern IN_LIST = Pattern.compile("([\\w]+)\\.([\\w`]+)\\s+in\\s*\\(([^)]*)\\)",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern RANGE = Pattern.compile("([\\w]+)\\.([\\w`]+)\\s*(>=|<=|>|<)\\s*(\\d+(?:\\.\\d+)?)");
    private static final Pattern BETWEEN = Pattern.compile(
            "([\\w]+)\\.([\\w`]+)\\s+between\\s+(\\d+(?:\\.\\d+)?)\\s+and\\s+(\\d+(?:\\.\\d+)?)",
            Pattern.CASE_INSENSITIVE);

    /** 非限定列谓词（无表别名时使用） */
    private static final Pattern BARE_EQ_STR = Pattern.compile("(?<![\\w.])([\\w`]+)\\s*=\\s*'([^']*)'");
    private static final Pattern BARE_EQ_NUM = Pattern.compile("(?<![\\w.])([\\w`]+)\\s*=\\s*(\\d+)");
    private static final Pattern BARE_IN = Pattern.compile("(?<![\\w.])([\\w`]+)\\s+in\\s*\\(([^)]*)\\)",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern BARE_RANGE = Pattern.compile("(?<![\\w.])([\\w`]+)\\s*(>=|<=|>|<)\\s*(\\d+(?:\\.\\d+)?)");

    /** 表别名：from/join t1 a */
    private static final Pattern TABLE_ALIAS = Pattern.compile(
            "(?:from|join)\\s+([`\\w.]+)\\s+(?:as\\s+)?([\\w]+)", Pattern.CASE_INSENSITIVE);

    private static final Pattern QUOTED = Pattern.compile("'([^']*)'");

    /**
     * 抽取目标表的谓词。
     *
     * @param sql         SQL 文本
     * @param targetTable 目标表名（小写）
     * @return 谓词列表（去重按列 + 算子）
     */
    public static List<EndpointTableFact.Predicate> extract(String sql, String targetTable) {
        List<EndpointTableFact.Predicate> out = new ArrayList<>();
        if (sql == null || sql.isBlank() || targetTable == null) {
            return out;
        }
        try {
            String table = targetTable.toLowerCase(Locale.ROOT);
            Set<String> allowed = aliasesOf(sql, table);

            // 表限定谓词
            matchEq(out, sql, EQ_STR, allowed, false);
            matchEq(out, sql, EQ_NUM, allowed, true);
            matchIn(out, sql, IN_LIST, allowed);
            matchRange(out, sql, RANGE, allowed);
            matchBetween(out, sql, BETWEEN, allowed);

            // 无别名时才接受非限定谓词（避免把别的表/别名列的谓词算到本表头上）
            if (!hasAnyAlias(sql)) {
                matchBareEq(out, sql, BARE_EQ_STR, false);
                matchBareEq(out, sql, BARE_EQ_NUM, true);
                matchBareIn(out, sql, BARE_IN);
                matchBareRange(out, sql, BARE_RANGE);
            }
        } catch (Exception ignore) {
            // 容错：静默
        }
        return out;
    }

    /** SQL 中是否存在表别名（from t a / join t b） */
    static boolean hasAnyAlias(String sql) {
        Matcher m = TABLE_ALIAS.matcher(sql.toLowerCase(Locale.ROOT));
        while (m.find()) {
            String alias = m.group(2);
            if (!isKeyword(alias)) {
                return true;
            }
        }
        return false;
    }

    /** 目标表允许出现的限定符集合：表名自身 + 绑定到该表的别名 */
    private static Set<String> aliasesOf(String sql, String table) {
        Set<String> allowed = new HashSet<>();
        allowed.add(table);
        String lower = sql.toLowerCase(Locale.ROOT);
        Matcher m = TABLE_ALIAS.matcher(lower);
        while (m.find()) {
            String rawTable = m.group(1).replace("`", "");
            int dot = rawTable.lastIndexOf('.');
            String simple = (dot >= 0 ? rawTable.substring(dot + 1) : rawTable);
            if (simple.equals(table)) {
                allowed.add(m.group(2));
            }
        }
        return allowed;
    }

    private static void matchEq(List<EndpointTableFact.Predicate> out, String sql, Pattern p,
                                Set<String> allowed, boolean numeric) {
        Matcher m = p.matcher(sql);
        while (m.find()) {
            if (!allowed.contains(m.group(1).toLowerCase(Locale.ROOT))) {
                continue;
            }
            Object v = numeric ? toNumber(m.group(3)) : m.group(3);
            if (v == null) {
                continue;
            }
            addEq(out, column(m.group(2)), v);
        }
    }

    private static void matchIn(List<EndpointTableFact.Predicate> out, String sql, Pattern p, Set<String> allowed) {
        Matcher m = p.matcher(sql);
        while (m.find()) {
            if (!allowed.contains(m.group(1).toLowerCase(Locale.ROOT))) {
                continue;
            }
            List<Object> values = parseValueList(m.group(3));
            if (values.isEmpty()) {
                continue;
            }
            EndpointTableFact.Predicate pred = new EndpointTableFact.Predicate();
            pred.setColumn(column(m.group(2)));
            pred.setOp(EndpointTableFact.Op.IN);
            pred.getValues().addAll(values);
            out.add(pred);
        }
    }

    private static void matchRange(List<EndpointTableFact.Predicate> out, String sql, Pattern p, Set<String> allowed) {
        Matcher m = p.matcher(sql);
        while (m.find()) {
            if (!allowed.contains(m.group(1).toLowerCase(Locale.ROOT))) {
                continue;
            }
            Double v = toDouble(m.group(4));
            if (v == null) {
                continue;
            }
            String op = m.group(3);
            EndpointTableFact.Predicate pred = new EndpointTableFact.Predicate();
            pred.setColumn(column(m.group(2)));
            pred.setOp(EndpointTableFact.Op.RANGE);
            if (">".equals(op) || ">=".equals(op)) {
                pred.setMin(v);
            } else {
                pred.setMax(v);
            }
            out.add(pred);
        }
    }

    private static void matchBetween(List<EndpointTableFact.Predicate> out, String sql, Pattern p, Set<String> allowed) {
        Matcher m = p.matcher(sql);
        while (m.find()) {
            if (!allowed.contains(m.group(1).toLowerCase(Locale.ROOT))) {
                continue;
            }
            Double lo = toDouble(m.group(3));
            Double hi = toDouble(m.group(4));
            if (lo == null || hi == null) {
                continue;
            }
            EndpointTableFact.Predicate pred = new EndpointTableFact.Predicate();
            pred.setColumn(column(m.group(2)));
            pred.setOp(EndpointTableFact.Op.RANGE);
            pred.setMin(lo);
            pred.setMax(hi);
            out.add(pred);
        }
    }

    private static void matchBareEq(List<EndpointTableFact.Predicate> out, String sql, Pattern p, boolean numeric) {
        Matcher m = p.matcher(sql);
        while (m.find()) {
            String col = column(m.group(1));
            if (isKeyword(col)) {
                continue;
            }
            Object v = numeric ? toNumber(m.group(2)) : m.group(2);
            if (v == null) {
                continue;
            }
            addEq(out, col, v);
        }
    }

    private static void matchBareIn(List<EndpointTableFact.Predicate> out, String sql, Pattern p) {
        Matcher m = p.matcher(sql);
        while (m.find()) {
            String col = column(m.group(1));
            if (isKeyword(col)) {
                continue;
            }
            List<Object> values = parseValueList(m.group(2));
            if (values.isEmpty()) {
                continue;
            }
            EndpointTableFact.Predicate pred = new EndpointTableFact.Predicate();
            pred.setColumn(col);
            pred.setOp(EndpointTableFact.Op.IN);
            pred.getValues().addAll(values);
            out.add(pred);
        }
    }

    private static void matchBareRange(List<EndpointTableFact.Predicate> out, String sql, Pattern p) {
        Matcher m = p.matcher(sql);
        while (m.find()) {
            String col = column(m.group(1));
            if (isKeyword(col)) {
                continue;
            }
            Double v = toDouble(m.group(3));
            if (v == null) {
                continue;
            }
            String op = m.group(2);
            EndpointTableFact.Predicate pred = new EndpointTableFact.Predicate();
            pred.setColumn(col);
            pred.setOp(EndpointTableFact.Op.RANGE);
            if (">".equals(op) || ">=".equals(op)) {
                pred.setMin(v);
            } else {
                pred.setMax(v);
            }
            out.add(pred);
        }
    }

    private static void addEq(List<EndpointTableFact.Predicate> out, String col, Object value) {
        for (EndpointTableFact.Predicate p : out) {
            if (p.getOp() == EndpointTableFact.Op.EQ && col.equals(p.getColumn())) {
                if (!p.getValues().contains(value)) {
                    p.getValues().add(value);
                }
                return;
            }
        }
        EndpointTableFact.Predicate pred = new EndpointTableFact.Predicate();
        pred.setColumn(col);
        pred.setOp(EndpointTableFact.Op.EQ);
        pred.getValues().add(value);
        out.add(pred);
    }

    /** 解析 IN 列表：优先字符串字面量，其次数值 */
    private static List<Object> parseValueList(String raw) {
        List<Object> values = new ArrayList<>();
        Matcher q = QUOTED.matcher(raw);
        boolean any = false;
        while (q.find()) {
            values.add(q.group(1));
            any = true;
        }
        if (any) {
            return values;
        }
        for (String part : raw.split(",")) {
            String s = part.trim();
            if (s.matches("\\d+")) {
                values.add(Long.parseLong(s));
            }
        }
        return values;
    }

    private static String column(String raw) {
        return raw == null ? "" : raw.replace("`", "").toLowerCase(Locale.ROOT);
    }

    private static Object toNumber(String s) {
        if (s == null || s.isEmpty()) {
            return null;
        }
        try {
            return Long.parseLong(s);
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private static Double toDouble(String s) {
        if (s == null || s.isEmpty()) {
            return null;
        }
        try {
            return Double.parseDouble(s);
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private static boolean isKeyword(String s) {
        return SqlTableExtractor.isKeywordCandidate(s);
    }
}
