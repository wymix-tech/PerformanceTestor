package com.perftestor.datagen.orm.support;

import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.EnumDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.expr.StringLiteralExpr;
import com.perftestor.datagen.model.MagicHint;
import com.perftestor.datagen.spi.OrmSourceParser;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Java 源码语义扫描公共片段：枚举常量反查、状态常量魔法值、Java 类名 → 表名转换。
 *
 * <p>MyBatis 与 JPA 解析器共用（ORM 语法差异仅体现在关联声明上，枚举/常量发现一致），
 * 避免两份实现漂移。
 */
public final class JavaSourceHints {

    private JavaSourceHints() {
    }

    /**
     * 收集 Java 枚举常量与 static final 状态常量（魔法值候选）。
     *
     * @param cu        已解析的编译单元
     * @param sourceRef 来源定位（文件:行号 或文件路径）
     * @param result    输出容器
     */
    public static void collectEnumsAndConstants(CompilationUnit cu, String sourceRef,
                                                OrmSourceParser.ScanResult result) {
        if (cu == null) {
            return;
        }
        // Java 枚举常量反查（枚举发现第四级来源）
        cu.findAll(EnumDeclaration.class).forEach(ed -> {
            List<String> constants = new ArrayList<>();
            ed.getEntries().forEach(e -> constants.add(e.getNameAsString()));
            if (!constants.isEmpty()) {
                result.getJavaEnums().put(ed.getNameAsString(), constants);
            }
        });

        // static final 常量：STATUS_X/TYPE_X 等命名特征，值入魔法值候选
        cu.findAll(FieldDeclaration.class).forEach(fd -> {
            if (!fd.isStatic() || !fd.isFinal()) {
                return;
            }
            fd.getVariables().forEach(vd -> {
                String name = vd.getNameAsString();
                if (!name.matches("(?i)(STATUS|STATE|TYPE|KIND|FLAG|LEVEL)[A-Z0-9_]*")) {
                    return;
                }
                vd.getInitializer().ifPresent(init -> {
                    MagicHint h = null;
                    if (init instanceof StringLiteralExpr s) {
                        h = new MagicHint(null, snake(name), MagicHint.KIND_CONSTANT);
                        h.getValues().add(s.getValue());
                    } else if (init instanceof com.github.javaparser.ast.expr.IntegerLiteralExpr
                            || init instanceof com.github.javaparser.ast.expr.LongLiteralExpr) {
                        try {
                            long v = Long.parseLong(init.toString());
                            h = new MagicHint(null, snake(name), MagicHint.KIND_CONSTANT);
                            h.getValues().add(v);
                        } catch (NumberFormatException ignore) {
                            return;
                        }
                    }
                    if (h != null) {
                        h.getSourceRefs().add(sourceRef);
                        result.getMagicHints().add(h);
                    }
                });
            });
        });
    }

    /** 常量名 → 列名（STATUS_SHIPPED → status；USER_TYPE_A → type） */
    public static String snake(String constantName) {
        String s = constantName.replaceAll("([a-z0-9])([A-Z])", "$1_$2").toLowerCase(Locale.ROOT);
        String[] tokens = s.split("_");
        for (String t : tokens) {
            if (t.equals("status") || t.equals("state") || t.equals("type")
                    || t.equals("flag") || t.equals("level") || t.equals("kind")) {
                return t;
            }
        }
        return s;
    }

    /** Java 类名/全限定名 → 表名（camelCase → snake_case，剥离 DO/Entity/PO 后缀） */
    public static String toTableName(String javaType) {
        if (javaType == null || javaType.isBlank()) {
            return null;
        }
        String simple = javaType.contains(".")
                ? javaType.substring(javaType.lastIndexOf('.') + 1) : javaType;
        simple = simple.replace("DO", "").replace("Entity", "").replace("PO", "");
        if (simple.isEmpty()) {
            return null;
        }
        return simple.replaceAll("([a-z0-9])([A-Z])", "$1_$2").toLowerCase(Locale.ROOT);
    }
}
