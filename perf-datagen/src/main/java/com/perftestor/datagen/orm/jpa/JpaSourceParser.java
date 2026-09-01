package com.perftestor.datagen.orm.jpa;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.expr.ArrayInitializerExpr;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.MemberValuePair;
import com.github.javaparser.ast.expr.NormalAnnotationExpr;
import com.github.javaparser.ast.expr.SingleMemberAnnotationExpr;
import com.github.javaparser.ast.expr.StringLiteralExpr;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.ast.type.Type;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SourceFile;
import com.perftestor.datagen.orm.support.JavaSourceHints;
import com.perftestor.datagen.spi.OrmSourceParser;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;

/**
 * JPA 源码解析器：解析 {@code @Entity/@Table/@ManyToOne/@OneToMany/@JoinColumn/@JoinTable}
 * 构建关联语义 {@link RelationHint}，与 {@code MyBatisSourceParser} 并列作为 ORM 扩展点实现。
 *
 * <p>覆盖范围：
 * <ul>
 *   <li>{@code @ManyToOne/@OneToOne} + {@code @JoinColumn}：子表外键列 → 父表主键（orm_join，置信 0.9）</li>
 *   <li>{@code @OneToMany/@ManyToMany} + {@code @JoinTable}：中间表两侧外键（orm_join，置信 0.9）</li>
 *   <li>{@code @OneToMany(mappedBy=...)}：反向声明，由对端 ManyToOne 覆盖，不重复产出</li>
 *   <li>Java 枚举与状态常量：复用 {@link JavaSourceHints}（与 MyBatis 一致）</li>
 * </ul>
 *
 * <p>容错策略同 MyBatis：单文件解析失败静默跳过，绝不中断灌数流程。
 */
public class JpaSourceParser implements OrmSourceParser {

    private static final Logger log = LoggerFactory.getLogger(JpaSourceParser.class);

    /** 单文件解析超时（ms） */
    static final long FILE_TIMEOUT_MS = 5_000;
    /** jakarta / javax 持久化包特征 */
    private static final String[] JPA_PACKAGES = {"jakarta.persistence.", "javax.persistence."};

    private final JavaParser parser = new JavaParser();

    @Override
    public boolean supports(List<SourceFile> files) {
        if (files == null) {
            return false;
        }
        for (SourceFile f : files) {
            String c = f.getContent();
            if (c == null) {
                continue;
            }
            for (String pkg : JPA_PACKAGES) {
                if (c.contains(pkg)) {
                    return true;
                }
            }
        }
        return false;
    }

    @Override
    public int order() {
        return 20;
    }

    @Override
    public ScanResult scan(List<SourceFile> files, Collection<String> knownTables) {
        ScanResult result = new ScanResult();
        long deadline = System.currentTimeMillis() + FILE_TIMEOUT_MS * Math.max(1, files.size() / 20 + 1);
        for (SourceFile f : files) {
            if (System.currentTimeMillis() > deadline) {
                log.warn("JPA 扫描整体超时，剩余文件跳过: {}", f.getPath());
                break;
            }
            if (f.getPath() == null || !f.getPath().toLowerCase(Locale.ROOT).endsWith(".java")) {
                continue;
            }
            try {
                scanJava(f, knownTables, result);
            } catch (Exception e) {
                // 单文件失败静默跳过
                log.debug("skip source file {}: {}", f.getPath(), e.getMessage());
            }
        }
        return result;
    }

    private void scanJava(SourceFile f, Collection<String> knownTables, ScanResult result) throws Exception {
        var parseResult = parser.parse(f.getContent());
        if (!parseResult.isSuccessful() || parseResult.getResult().isEmpty()) {
            return;
        }
        CompilationUnit cu = parseResult.getResult().get();
        String sourceRef = f.getPath();

        // 枚举与常量（与 MyBatis 共用片段）
        JavaSourceHints.collectEnumsAndConstants(cu, sourceRef, result);

        // 第一遍：实体类名 → 表名（@Table(name) 优先，否则类名 snake_case）
        Map<String, String> entityTables = new HashMap<>();
        List<ClassOrInterfaceDeclaration> entities = new ArrayList<>();
        for (ClassOrInterfaceDeclaration cd : cu.findAll(ClassOrInterfaceDeclaration.class)) {
            if (!hasAnnotation(cd, "Entity")) {
                continue;
            }
            entities.add(cd);
            String table = annotationString(cd, "Table", "name");
            if (table == null || table.isBlank()) {
                table = JavaSourceHints.toTableName(cd.getNameAsString());
            }
            if (table != null) {
                entityTables.put(cd.getNameAsString(), table.toLowerCase(Locale.ROOT));
            }
        }

        // 第二遍：关联声明
        for (ClassOrInterfaceDeclaration cd : entities) {
            String childTable = entityTables.get(cd.getNameAsString());
            if (childTable == null) {
                continue;
            }
            for (FieldDeclaration fd : cd.getFields()) {
                if (fd.getVariables().isEmpty()) {
                    continue;
                }
                String fieldName = fd.getVariable(0).getNameAsString();
                String targetEntity = targetEntityName(fd);
                if (targetEntity == null) {
                    continue;
                }
                String parentTable = entityTables.getOrDefault(
                        targetEntity, JavaSourceHints.toTableName(targetEntity));

                if (hasAnnotation(fd, "ManyToOne") || hasAnnotation(fd, "OneToOne")) {
                    String[] join = parseJoinColumn(annotationValue(fd, "JoinColumn"));
                    String column = join[0] != null ? join[0] : JavaSourceHints.snake(fieldName) + "_id";
                    String parentColumn = join[1] != null ? join[1] : "id";
                    addHint(result, knownTables, childTable, column, parentTable, parentColumn, sourceRef);
                } else if (hasAnnotation(fd, "OneToMany") || hasAnnotation(fd, "ManyToMany")) {
                    AnnotationExpr joinTable = annotation(fd, "JoinTable").orElse(null);
                    if (joinTable == null) {
                        continue; // mappedBy 反向声明：由对端 ManyToOne 覆盖
                    }
                    String jtName = stringMember(joinTable, "name");
                    if (jtName == null || jtName.isBlank()) {
                        continue;
                    }
                    String joinTable_ = jtName.toLowerCase(Locale.ROOT);
                    String[] ownerSide = parseJoinColumn(memberValue(joinTable, "joinColumns"));
                    String[] targetSide = parseJoinColumn(memberValue(joinTable, "inverseJoinColumns"));
                    // 中间表 → 拥有方（本实体表）
                    addHint(result, knownTables, joinTable_,
                            ownerSide[0] != null ? ownerSide[0] : JavaSourceHints.snake(childTable) + "_id",
                            childTable, ownerSide[1] != null ? ownerSide[1] : "id", sourceRef);
                    // 中间表 → 目标方
                    addHint(result, knownTables, joinTable_,
                            targetSide[0] != null ? targetSide[0] : JavaSourceHints.snake(parentTable) + "_id",
                            parentTable, targetSide[1] != null ? targetSide[1] : "id", sourceRef);
                }
            }
        }
    }

    private void addHint(ScanResult result, Collection<String> knownTables, String childTable, String childColumn,
                         String parentTable, String parentColumn, String sourceRef) {
        if (childTable == null || parentTable == null) {
            return;
        }
        if (!knownTables.contains(childTable) || !knownTables.contains(parentTable)) {
            return;
        }
        RelationHint h = new RelationHint(childTable, childColumn, parentTable, parentColumn,
                RelationHint.SRC_ORM_JOIN, 0.9);
        h.getSourceRefs().add(sourceRef);
        result.getRelationHints().add(h);
    }

    /** 字段的目标实体类名（ManyToOne 取类型；List<X>/Set<X> 取泛型首个实参） */
    private String targetEntityName(FieldDeclaration fd) {
        Type type = fd.getElementType();
        if (type instanceof ClassOrInterfaceType ct) {
            if (ct.getTypeArguments().isPresent() && !ct.getTypeArguments().get().isEmpty()) {
                return ct.getTypeArguments().get().get(0).asString();
            }
            return ct.getNameAsString();
        }
        return null;
    }

    // ===== 注解取值工具 =====

    private boolean hasAnnotation(NodeWithAnnotations<?> node, String name) {
        return annotation(node, name).isPresent();
    }

    private Optional<AnnotationExpr> annotation(NodeWithAnnotations<?> node, String name) {
        for (AnnotationExpr a : node.getAnnotations()) {
            if (name.equals(a.getNameAsString())) {
                return Optional.of(a);
            }
        }
        return Optional.empty();
    }

    /** 取单值注解成员（如 @Table 的 name） */
    private String annotationString(ClassOrInterfaceDeclaration cd, String ann, String key) {
        Optional<AnnotationExpr> a = annotation(cd, ann);
        return a.map(v -> stringMember(v, key)).orElse(null);
    }

    /** @JoinColumn 的值表达式（供 parseJoinColumn 解析） */
    private Expression annotationValue(FieldDeclaration fd, String ann) {
        return annotation(fd, ann).orElse(null);
    }

    /** 注解成员值表达式（name / joinColumns / inverseJoinColumns） */
    private Expression memberValue(AnnotationExpr ann, String key) {
        if (ann instanceof NormalAnnotationExpr n) {
            for (MemberValuePair p : n.getPairs()) {
                if (key.equals(p.getNameAsString())) {
                    return p.getValue();
                }
            }
        }
        return null;
    }

    private String stringMember(AnnotationExpr ann, String key) {
        if (ann instanceof SingleMemberAnnotationExpr s && "name".equals(key)) {
            return literal(s.getMemberValue());
        }
        Expression v = memberValue(ann, key);
        return v == null ? null : literal(v);
    }

    private String literal(Expression e) {
        return e instanceof StringLiteralExpr s ? s.getValue() : null;
    }

    /** {@code @JoinColumn(name="user_id", referencedColumnName="id")} → {name, referencedColumnName} */
    private String[] parseJoinColumn(Expression e) {
        String[] out = {null, null};
        if (e == null) {
            return out;
        }
        if (e instanceof SingleMemberAnnotationExpr s) {
            out[0] = literal(s.getMemberValue());
            return out;
        }
        if (e instanceof NormalAnnotationExpr n) {
            for (MemberValuePair p : n.getPairs()) {
                if ("name".equals(p.getNameAsString())) {
                    out[0] = literal(p.getValue());
                } else if ("referencedColumnName".equals(p.getNameAsString())) {
                    out[1] = literal(p.getValue());
                }
            }
            return out;
        }
        // joinColumns = { @JoinColumn(name = "...") }
        if (e instanceof ArrayInitializerExpr arr) {
            for (Expression item : arr.getValues()) {
                if (item instanceof NormalAnnotationExpr || item instanceof SingleMemberAnnotationExpr) {
                    return parseJoinColumn(item);
                }
            }
        }
        return out;
    }
}
