package com.perftestor.datagen.util;

import com.perftestor.datagen.model.MagicHint;
import com.perftestor.datagen.model.RelationHint;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * SQL 文本容错抽取器：从 SQL 字符串中提取魔法值字面量与 JOIN 关联。
 * 任何解析失败静默返回空结果，绝不抛出异常中断灌数（SQL 碎片化防御）。
 */
public final class SqlLiteralExtractor {

    private SqlLiteralExtractor() {
    }

    /** 相等谓词：a.col = 'v' / a.col = 123（别名.列名.值 三段捕获） */
    private static final Pattern EQ_STR = Pattern.compile("([\\w]+)\\.([\\w`]+)\\s*=\\s*'([^']*)'");
    private static final Pattern EQ_NUM = Pattern.compile("([\\w]+)\\.([\\w`]+)\\s*=\\s*(\\d+)");
    /** IN 列表：a.col IN (1,2) / a.col IN ('a','b') */
    private static final Pattern IN_LIST = Pattern.compile("([\\w]+)\\.([\\w`]+)\\s+in\\s*\\(([^)]*)\\)",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern QUOTED = Pattern.compile("'([^']*)'");
    /** JOIN ON：join t2 b on a.col1 = b.col2（左连接同样命中） */
    private static final Pattern JOIN_ON = Pattern.compile(
            "join\\s+([\\w]+)\\s+([\\w]+)\\s+on\\s+([\\w]+)\\.([\\w`]+)\\s*=\\s*([\\w]+)\\.([\\w`]+)",
            Pattern.CASE_INSENSITIVE);
    /** 表别名：from/join t1 a（JOIN 子句别名同样进入映射） */
    private static final Pattern TABLE_ALIAS = Pattern.compile(
            "(?:from|join)\\s+([\\w]+)\\s+(?:as\\s+)?([\\w]+)", Pattern.CASE_INSENSITIVE);

    /**
     * 从 SQL 提取指定表的魔法值候选。
     *
     * @param sql         SQL 文本（可为 ${} 碎片，失败静默）
     * @param targetTable 目标表（小写；列名归属需有表限定符命中该表或其别名）
     */
    public static List<MagicHint> extractLiterals(String sql, String targetTable, String sourceRef) {
        List<MagicHint> hints = new ArrayList<>();
        if (sql == null || sql.isBlank() || targetTable == null) {
            return hints;
        }
        try {
            String t = targetTable.toLowerCase(Locale.ROOT);
            java.util.Set<String> allowed = new java.util.HashSet<>();
            allowed.add(t);
            Matcher ta = TABLE_ALIAS.matcher(sql);
            while (ta.find()) {
                if (ta.group(1).toLowerCase(Locale.ROOT).equals(t)) {
                    allowed.add(ta.group(2).toLowerCase(Locale.ROOT));
                }
            }
            // 相等谓词（字符串/数值）
            addEq(hints, sql, EQ_STR, allowed, targetTable, sourceRef, false);
            addEq(hints, sql, EQ_NUM, allowed, targetTable, sourceRef, true);
            // IN 列表
            Matcher in = IN_LIST.matcher(sql);
            while (in.find()) {
                if (!allowed.contains(in.group(1).toLowerCase(Locale.ROOT))) {
                    continue;
                }
                List<Object> values = new ArrayList<>();
                Matcher q = QUOTED.matcher(in.group(3));
                if (q.find()) {
                    do {
                        values.add(q.group(1));
                    } while (q.find());
                } else {
                    for (String part : in.group(3).split(",")) {
                        String p = part.trim();
                        if (p.matches("\\d+")) {
                            values.add(Long.parseLong(p));
                        }
                    }
                }
                addHint(hints, targetTable, in.group(2), values, sourceRef);
            }
        } catch (Exception ignore) {
            // 容错：静默
        }
        return hints;
    }

    /** 相等谓词收集：group1=别名（须命中 allowed）、group2=列名、group3=值 */
    private static void addEq(List<MagicHint> hints, String sql, Pattern p,
                              java.util.Set<String> allowed, String targetTable,
                              String sourceRef, boolean numeric) {
        Matcher m = p.matcher(sql);
        while (m.find()) {
            if (!allowed.contains(m.group(1).toLowerCase(Locale.ROOT))) {
                continue;
            }
            try {
                Object v = numeric ? Long.parseLong(m.group(3)) : m.group(3);
                addHint(hints, targetTable, m.group(2), List.of(v), sourceRef);
            } catch (NumberFormatException ignore) {
                // 容错：静默
            }
        }
    }

    private static void addHint(List<MagicHint> hints, String table, String column,
                                List<Object> values, String sourceRef) {
        MagicHint h = new MagicHint(table, column.replace("`", ""), MagicHint.KIND_LITERAL);
        h.getValues().addAll(values);
        if (sourceRef != null) {
            h.getSourceRefs().add(sourceRef);
        }
        hints.add(h);
    }

    /** 从 JOIN ON 提取关联边（归属 knownTables 内的表） */
    public static List<RelationHint> extractJoinRelations(String sql, Collection<String> knownTables) {
        List<RelationHint> hints = new ArrayList<>();
        if (sql == null || sql.isBlank()) {
            return hints;
        }
        try {
            String lower = sql.toLowerCase(Locale.ROOT);
            java.util.Set<String> known = new java.util.HashSet<>();
            for (String t : knownTables) {
                known.add(t.toLowerCase(Locale.ROOT));
            }
            // alias → table（from 与 join 子句的别名均进入映射）
            java.util.Map<String, String> aliasTable = new java.util.HashMap<>();
            Matcher ta = TABLE_ALIAS.matcher(lower);
            while (ta.find()) {
                aliasTable.put(ta.group(2), ta.group(1));
            }
            Matcher j = JOIN_ON.matcher(lower);
            while (j.find()) {
                String t1 = aliasTable.getOrDefault(j.group(3), j.group(3));
                String t2 = aliasTable.getOrDefault(j.group(5), j.group(5));
                if (!known.contains(t1) || !known.contains(t2) || t1.equals(t2)) {
                    continue;
                }
                String c1 = j.group(4).replace("`", "");
                String c2 = j.group(6).replace("`", "");
                // 约定：含 _id 后缀的列一侧为子表
                RelationHint h = buildEdge(t1, c1, t2, c2);
                if (h != null) {
                    h.getSourceRefs().add("sql-join(regex)");
                    hints.add(h);
                }
            }
        } catch (Exception ignore) {
            // 容错：静默
        }
        return hints;
    }

    private static RelationHint buildEdge(String t1, String c1, String t2, String c2) {
        boolean c1IsFk = c1.endsWith("_id");
        boolean c2IsFk = c2.endsWith("_id");
        if (c1IsFk && !c2IsFk) {
            return new RelationHint(t1, c1, t2, c2, RelationHint.SRC_ORM_JOIN, 0.9);
        }
        if (c2IsFk && !c1IsFk) {
            return new RelationHint(t2, c2, t1, c1, RelationHint.SRC_ORM_JOIN, 0.9);
        }
        // 两端皆含/皆不含 _id：按表名匹配列前缀判定（user_id ↔ user.id）
        if (t2.equals(c1.replace("_id", ""))) {
            return new RelationHint(t1, c1, t2, c2, RelationHint.SRC_ORM_JOIN, 0.9);
        }
        if (t1.equals(c2.replace("_id", ""))) {
            return new RelationHint(t2, c2, t1, c1, RelationHint.SRC_ORM_JOIN, 0.9);
        }
        return null;
    }
}
