package com.perftestor.datagen.enumv;

import com.perftestor.datagen.model.SchemaMetadata;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 枚举值四级发现（优先级递减）：
 * 1. MySQL ENUM 列类型（TYPE_NAME 解析，方言 getEnumValues 承接）
 * 2. CHECK 约束（information_schema，MySQL 8.0.16+ 通用 SQL）
 * 3. 列注释模式「状态 1=待支付 2=已支付」正则解析
 * 4. Java 枚举反查（MyBatis 源码扫描产物，列名 ↔ 枚举类名模糊匹配）
 *
 * 全部来源均未命中时返回空（引擎维持类型默认生成，并将列标记为疑似枚举列交前端提示）。
 */
public class EnumValueDiscoverer {

    /** 注释枚举模式：「1=待支付 2=已支付」/「PENDING=待支付」键值对（容忍全角标点分隔） */
    private static final Pattern COMMENT_PAIR = Pattern.compile("([A-Za-z0-9_\\-]+)\\s*[=＝]\\s*([^\\s,;，；、]+)");
    /** CHECK 约束中 col IN ('a','b') 或 col = 'a' 的字面量提取 */
    private static final Pattern CHECK_IN = Pattern.compile("[\\w`\"\\[\\]]+\\s+in\\s*\\(([^)]+)\\)", Pattern.CASE_INSENSITIVE);
    private static final Pattern CHECK_EQ = Pattern.compile("[\\w`\"\\[\\]]+\\s*=\\s*'([^']*)'");
    private static final Pattern QUOTED = Pattern.compile("'([^']*)'");
    /** 疑似枚举列名（值发现失败时提示前端人工确认） */
    private static final Pattern SUSPECT_NAME = Pattern.compile(".*(status|state|type|flag|level|kind|category).*");

    /**
     * 发现枚举候选值。
     *
     * @param javaEnums ORM 扫描产物（枚举类名 → 常量），可空
     * @return 候选值列表（保持声明顺序）；空列表表示未发现
     */
    public List<Object> discover(Connection conn, com.perftestor.datagen.spi.DatabaseDialect dialect,
                                 SchemaMetadata.ColumnMeta col, Map<String, List<String>> javaEnums) {
        return discover(conn, dialect, null, col, javaEnums);
    }

    /**
     * 带表名的枚举发现（DB 级查询需要表名：MySQL information_schema / PG pg_enum）。
     *
     * @param tableName 列所属表名（小写；可空，空则跳过 DB 级查询）
     */
    public List<Object> discover(Connection conn, com.perftestor.datagen.spi.DatabaseDialect dialect,
                                 String tableName, SchemaMetadata.ColumnMeta col,
                                 Map<String, List<String>> javaEnums) {
        List<Object> result = new ArrayList<>();

        // 级别 1：DB ENUM 类型（MySQL TYPE_NAME=ENUM('A','B')；PG pg_enum/CHECK 由方言实现）
        try {
            if (conn != null && dialect != null && tableName != null && !tableName.isBlank()) {
                List<String> dbVals = dialect.getEnumValues(conn, tableName, col.getName());
                if (!dbVals.isEmpty()) {
                    result.addAll(dbVals);
                    return result;
                }
            }
        } catch (Exception ignore) {
            // 方言查询失败降级后续来源
        }
        // TYPE_NAME 内联 ENUM 解析（不依赖连接）
        List<String> inline = parseInlineEnum(col.getTypeName());
        if (!inline.isEmpty()) {
            result.addAll(inline);
            return result;
        }

        // 级别 2：CHECK 约束（需要连接与方言支持，调用方可预取 clause 传入重载）
        // （本方法由重载版本携带 checkClause；此处仅注释级与枚举反查）

        // 级别 3：列注释「1=待支付 2=已支付」
        List<Object> fromComment = parseComment(col.getComment(), col);
        if (!fromComment.isEmpty()) {
            result.addAll(fromComment);
            return result;
        }

        // 级别 4：Java 枚举反查（order_status ↔ OrderStatus）
        if (javaEnums != null && !javaEnums.isEmpty()) {
            List<String> candidates = matchJavaEnum(col.getName(), javaEnums);
            if (!candidates.isEmpty()) {
                result.addAll(candidates);
                return result;
            }
        }
        return result;
    }

    /** 带 CHECK 子句的重载（宿主可预取 information_schema.CHECK_CONSTRAINTS 后传入） */
    public List<Object> discover(SchemaMetadata.ColumnMeta col, String checkClause,
                                 Map<String, List<String>> javaEnums) {
        // 级别 1 内联 ENUM
        List<String> inline = parseInlineEnum(col.getTypeName());
        if (!inline.isEmpty()) {
            return new ArrayList<>(inline);
        }
        // 级别 2：CHECK 约束字面量
        List<Object> fromCheck = parseCheckClause(checkClause);
        if (!fromCheck.isEmpty()) {
            return fromCheck;
        }
        // 级别 3：注释
        List<Object> fromComment = parseComment(col.getComment(), col);
        if (!fromComment.isEmpty()) {
            return fromComment;
        }
        // 级别 4：Java 枚举
        if (javaEnums != null) {
            List<String> candidates = matchJavaEnum(col.getName(), javaEnums);
            if (!candidates.isEmpty()) {
                return new ArrayList<>(candidates);
            }
        }
        return new ArrayList<>();
    }

    /** MySQL ENUM('A','B') 类型名解析 */
    public static List<String> parseInlineEnum(String typeName) {
        List<String> vals = new ArrayList<>();
        if (typeName == null) {
            return vals;
        }
        String t = typeName.trim();
        String upper = t.toUpperCase(Locale.ROOT);
        if (!upper.startsWith("ENUM(") && !upper.startsWith("SET(")) {
            return vals;
        }
        Matcher m = QUOTED.matcher(t);
        while (m.find()) {
            vals.add(m.group(1));
        }
        return vals;
    }

    /** CHECK 子句字面量提取：col IN ('a','b') / col = 'x' / 数值列 IN (1,2) */
    public static List<Object> parseCheckClause(String clause) {
        List<Object> vals = new ArrayList<>();
        if (clause == null || clause.isBlank()) {
            return vals;
        }
        Matcher in = CHECK_IN.matcher(clause);
        String segment = null;
        while (in.find()) {
            segment = in.group(1);
            break;
        }
        if (segment == null) {
            Matcher eq = CHECK_EQ.matcher(clause);
            while (eq.find()) {
                vals.add(eq.group(1));
            }
            return vals;
        }
        Matcher q = QUOTED.matcher(segment);
        if (q.find()) {
            do {
                vals.add(q.group(1));
            } while (q.find());
        } else {
            for (String part : segment.split(",")) {
                String p = part.trim();
                if (!p.isEmpty()) {
                    vals.add(parseNumber(p));
                }
            }
        }
        return vals;
    }

    /** 注释键值对解析：整数族列取键（数字）；字符串列键为字母数字单词时取键 */
    public static List<Object> parseComment(String comment, SchemaMetadata.ColumnMeta col) {
        List<Object> vals = new ArrayList<>();
        if (comment == null || comment.isBlank()) {
            return vals;
        }
        Matcher m = COMMENT_PAIR.matcher(comment);
        int found = 0;
        while (m.find()) {
            String key = m.group(1);
            Object v = parseNumber(key);
            vals.add(v);
            found++;
        }
        // 至少 2 对才视为枚举注释，避免误报
        if (found < 2) {
            return new ArrayList<>();
        }
        return vals;
    }

    /** 列名 ↔ 枚举类名模糊匹配：order_status → OrderStatus / UserState → user_state */
    public static List<String> matchJavaEnum(String columnName, Map<String, List<String>> javaEnums) {
        if (columnName == null) {
            return List.of();
        }
        String col = columnName.toLowerCase(Locale.ROOT);
        for (Map.Entry<String, List<String>> e : javaEnums.entrySet()) {
            String cls = e.getKey();
            // 类名 → snake_case
            String snake = cls.replaceAll("([a-z0-9])([A-Z])", "$1_$2").toLowerCase(Locale.ROOT);
            if (col.equals(snake) || col.endsWith("_" + snake) || snake.endsWith("_" + col)
                    || col.replace("_", "").equals(snake.replace("_", ""))) {
                return e.getValue();
            }
        }
        return List.of();
    }

    /** 疑似枚举列（值发现失败但列名含 status/type/state/flag 等） */
    public static boolean suspectEnumColumn(SchemaMetadata.ColumnMeta col) {
        String n = col.getName() == null ? "" : col.getName().toLowerCase(Locale.ROOT);
        return SUSPECT_NAME.matcher(n).matches();
    }

    private static Object parseNumber(String s) {
        try {
            return Long.parseLong(s);
        } catch (NumberFormatException e) {
            return s;
        }
    }
}
