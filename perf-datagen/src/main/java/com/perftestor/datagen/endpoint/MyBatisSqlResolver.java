package com.perftestor.datagen.endpoint;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.expr.ArrayInitializerExpr;
import com.github.javaparser.ast.expr.BinaryExpr;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.MemberValuePair;
import com.github.javaparser.ast.expr.NormalAnnotationExpr;
import com.github.javaparser.ast.expr.SingleMemberAnnotationExpr;
import com.github.javaparser.ast.expr.StringLiteralExpr;
import com.perftestor.datagen.model.SourceFile;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

import javax.xml.parsers.DocumentBuilderFactory;
import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;

/**
 * Mapper 方法 → SQL 文本解析器（双通道）。
 *
 * <ul>
 *   <li>XML：按 {@code <mapper namespace>} + 语句 {@code id} 匹配（namespace 末段或全等匹配 Mapper 简单类名）</li>
 *   <li>注解：{@code @Select/@Insert/@Update/@Delete} 的字符串字面量（数组多段以空格拼接）</li>
 * </ul>
 *
 * <p>动态标签（{@code <if>/<where>/<foreach>}）取文本节点拼接——碎片化不会中断，
 * 缺失片段只会让结论不完整，由下游 {@code SqlTableExtractor} 的 warning 出口暴露，不做静默猜测。</p>
 */
public final class MyBatisSqlResolver {

    private static final Logger log = LoggerFactory.getLogger(MyBatisSqlResolver.class);

    private static final String[] SQL_ANNOTATIONS = {"Select", "Insert", "Update", "Delete"};
    private static final String[] STATEMENT_TAGS = {"select", "insert", "update", "delete", "sql"};

    private final List<SourceFile> xmlFiles;
    private final JavaParser parser = new JavaParser();

    /** 注解式：文件路径 → (方法名 → SQL) */
    private final Map<String, Map<String, String>> annotationSql = new HashMap<>();
    /** XML 式：namespace 末段 → (语句 id → SQL) */
    private final Map<String, Map<String, String>> xmlSql = new HashMap<>();
    private volatile boolean indexed;

    public MyBatisSqlResolver(List<SourceFile> xmlFiles) {
        this.xmlFiles = xmlFiles == null ? List.of() : xmlFiles;
    }

    /**
     * 解析 Mapper 方法的 SQL。
     *
     * @param mapperType Mapper 简单类名（如 OrderMapper）
     * @param methodName Mapper 方法名
     * @param mapperFile Mapper 源码文件路径（可空；非空时优先按注解解析）
     * @return SQL 文本；未定位到返回 empty
     */
    public Optional<String> resolve(String mapperType, String methodName, String mapperFile) {
        ensureIndexed();
        if (mapperFile != null && !mapperFile.isBlank()) {
            Map<String, String> byMethod = annotationSql.get(mapperFile);
            if (byMethod != null) {
                String sql = byMethod.get(methodName);
                if (sql != null && !sql.isBlank()) {
                    return Optional.of(sql);
                }
            }
        }
        if (mapperType != null) {
            Map<String, String> byId = xmlSql.get(mapperType.toLowerCase(Locale.ROOT));
            if (byId != null) {
                String sql = byId.get(methodName);
                if (sql != null && !sql.isBlank()) {
                    return Optional.of(sql);
                }
            }
        }
        return Optional.empty();
    }

    /** 按 Mapper 调用点解析（便捷重载） */
    public Optional<String> resolve(CallGraphAnalyzer.MapperInvocation inv) {
        if (inv == null) {
            return Optional.empty();
        }
        return resolve(inv.getMapperType(), inv.getMethodName(), inv.getMapperFile());
    }

    private synchronized void ensureIndexed() {
        if (indexed) {
            return;
        }
        indexed = true;
        for (SourceFile f : xmlFiles) {
            try {
                indexXml(f);
            } catch (Exception e) {
                // 单文件失败静默跳过（防 XML 碎片化中断整体解析）
                log.debug("skip mapper xml {}: {}", f.getPath(), e.getMessage());
            }
        }
    }

    private void indexXml(SourceFile f) throws Exception {
        String content = f.getContent();
        if (content == null || !content.contains("<mapper")) {
            return;
        }
        DocumentBuilderFactory dbf = DocumentBuilderFactory.newInstance();
        dbf.setFeature("http://apache.org/xml/features/nonvalidating/load-external-dtd", false);
        dbf.setNamespaceAware(false);
        Document doc = dbf.newDocumentBuilder()
                .parse(new ByteArrayInputStream(content.getBytes(StandardCharsets.UTF_8)));
        NodeList mappers = doc.getElementsByTagName("mapper");
        if (mappers.getLength() == 0) {
            return;
        }
        Element mapper = (Element) mappers.item(0);
        String namespace = mapper.getAttribute("namespace");
        if (namespace == null || namespace.isBlank()) {
            return;
        }
        String key = namespace.contains(".")
                ? namespace.substring(namespace.lastIndexOf('.') + 1) : namespace;
        Map<String, String> byId = xmlSql.computeIfAbsent(key.toLowerCase(Locale.ROOT), k -> new HashMap<>());
        for (String tag : STATEMENT_TAGS) {
            NodeList stmts = doc.getElementsByTagName(tag);
            for (int i = 0; i < stmts.getLength(); i++) {
                Element stmt = (Element) stmts.item(i);
                String id = stmt.getAttribute("id");
                if (id == null || id.isBlank()) {
                    continue;
                }
                byId.putIfAbsent(id, stmt.getTextContent());
            }
        }
    }

    /**
     * 解析 Mapper 接口的注解 SQL（宿主在拿到 Mapper 源码文件时调用）。
     *
     * @param mapperFiles Mapper 接口源码文件
     */
    public void indexAnnotations(List<SourceFile> mapperFiles) {
        if (mapperFiles == null) {
            return;
        }
        for (SourceFile f : mapperFiles) {
            try {
                indexAnnotationFile(f);
            } catch (Exception e) {
                log.debug("skip mapper java {}: {}", f.getPath(), e.getMessage());
            }
        }
    }

    private void indexAnnotationFile(SourceFile f) {
        if (f.getContent() == null) {
            return;
        }
        Optional<CompilationUnit> cuOpt = parser.parse(f.getContent()).getResult();
        if (cuOpt.isEmpty()) {
            return;
        }
        Map<String, String> byMethod = annotationSql.computeIfAbsent(f.getPath(), k -> new HashMap<>());
        for (MethodDeclaration m : cuOpt.get().findAll(MethodDeclaration.class)) {
            for (AnnotationExpr anno : m.getAnnotations()) {
                if (!isSqlAnnotation(anno.getNameAsString())) {
                    continue;
                }
                String sql = annotationText(anno);
                if (sql != null && !sql.isBlank()) {
                    byMethod.putIfAbsent(m.getNameAsString(), sql);
                }
            }
        }
    }

    private static boolean isSqlAnnotation(String name) {
        for (String a : SQL_ANNOTATIONS) {
            if (a.equals(name)) {
                return true;
            }
        }
        return false;
    }

    /** 取注解 SQL：单成员字符串、value 成员，或数组多段以空格拼接 */
    private static String annotationText(AnnotationExpr anno) {
        List<String> parts = new ArrayList<>();
        if (anno instanceof SingleMemberAnnotationExpr s) {
            collectStrings(s.getMemberValue(), parts);
        } else if (anno instanceof NormalAnnotationExpr n) {
            for (MemberValuePair p : n.getPairs()) {
                if ("value".equals(p.getNameAsString())) {
                    collectStrings(p.getValue(), parts);
                }
            }
        }
        return parts.isEmpty() ? null : String.join(" ", parts);
    }

    /**
     * 收集注解中的 SQL 字符串：字符串字面量、数组、以及 {@code "A" + "B"} 拼接
     * （perf-demo 及多数项目的注解 SQL 都是拼接写法，不处理会导致整条 SQL 丢失）。
     */
    private static void collectStrings(Expression e, List<String> out) {
        if (e instanceof StringLiteralExpr s) {
            out.add(s.getValue());
        } else if (e instanceof ArrayInitializerExpr arr) {
            for (Expression item : arr.getValues()) {
                collectStrings(item, out);
            }
        } else if (e instanceof BinaryExpr bin && bin.getOperator() == BinaryExpr.Operator.PLUS) {
            collectStrings(bin.getLeft(), out);
            collectStrings(bin.getRight(), out);
        }
    }
}
