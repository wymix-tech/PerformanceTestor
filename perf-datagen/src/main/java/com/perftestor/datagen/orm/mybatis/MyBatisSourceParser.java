package com.perftestor.datagen.orm.mybatis;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.EnumDeclaration;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.expr.MemberValuePair;
import com.github.javaparser.ast.expr.SingleMemberAnnotationExpr;
import com.github.javaparser.ast.expr.StringLiteralExpr;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.perftestor.datagen.model.MagicHint;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SourceFile;
import com.perftestor.datagen.spi.OrmSourceParser;
import com.perftestor.datagen.util.SqlLiteralExtractor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

import javax.xml.parsers.DocumentBuilderFactory;
import java.io.ByteArrayInputStream;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * MyBatis 源码解析器（本期唯一 ORM 实现）。
 *
 * <p>容错策略（防 SQL 碎片化）：</p>
 * <ul>
 *   <li>关联识别以 XML {@code <resultMap>} 的 {@code <association>/<collection>} 为主（结构化声明，成功率 100%）</li>
 *   <li>注解 SQL 的 JOIN 仅简单正则辅助，匹配失败静默跳过</li>
 *   <li>单文件解析超时 {@value #FILE_TIMEOUT_MS}ms 强制跳过，不中断灌数</li>
 * </ul>
 */
public class MyBatisSourceParser implements OrmSourceParser {

    private static final Logger log = LoggerFactory.getLogger(MyBatisSourceParser.class);

    /** 单文件解析超时（ms） */
    static final long FILE_TIMEOUT_MS = 5_000;
    /** mapper DTD 特征 */
    private static final String MYBATIS_DTD = "-//mybatis.org//DTD Mapper";
    /** @Select/@Insert/@Update 注解特征 */
    private static final Pattern ANNOTATION_SQL = Pattern.compile("@(Select|Insert|Update|Delete)\\s*\\(");

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
            if (f.getPath() != null && f.getPath().toLowerCase(Locale.ROOT).endsWith(".xml")
                    && c.contains(MYBATIS_DTD)) {
                return true;
            }
            if (c.contains("org.apache.ibatis") || ANNOTATION_SQL.matcher(c).find()) {
                return true;
            }
        }
        return false;
    }

    @Override
    public int order() {
        return 10;
    }

    @Override
    public ScanResult scan(List<SourceFile> files, Collection<String> knownTables) {
        ScanResult result = new ScanResult();
        long deadline = System.currentTimeMillis() + FILE_TIMEOUT_MS * Math.max(1, files.size() / 20 + 1);
        for (SourceFile f : files) {
            if (System.currentTimeMillis() > deadline) {
                log.warn("MyBatis 扫描整体超时，剩余文件跳过: {}", f.getPath());
                break;
            }
            try {
                if (f.getPath() != null && f.getPath().toLowerCase(Locale.ROOT).endsWith(".xml")) {
                    scanXml(f, knownTables, result, deadline);
                } else if (f.getPath() != null && f.getPath().toLowerCase(Locale.ROOT).endsWith(".java")) {
                    scanJava(f, knownTables, result, deadline);
                }
            } catch (Exception e) {
                // 单文件失败静默跳过（防 SQL 方言碎片化中断灌数）
                log.debug("skip source file {}: {}", f.getPath(), e.getMessage());
            }
        }
        return result;
    }

    /** Java 源码：@Select 注解 SQL 字面量/JOIN + static final 常量 + enum 常量反查 */
    private void scanJava(SourceFile f, Collection<String> knownTables, ScanResult result, long deadline) throws Exception {
        String content = f.getContent();
        var parseResult = parser.parse(content);
        if (!parseResult.isSuccessful() || parseResult.getResult().isEmpty()) {
            return;
        }
        CompilationUnit cu = parseResult.getResult().get();
        String sourceRef = f.getPath();

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

        // @Select/@Insert/@Update/@Delete 注解 SQL（字符串常量；provider 等形态静默跳过）
        for (String ann : new String[]{"Select", "Insert", "Update", "Delete"}) {
            cu.findAll(AnnotationExpr.class).forEach(a -> {
                if (!ann.equals(a.getNameAsString())) {
                    return;
                }
                String sql = null;
                if (a instanceof SingleMemberAnnotationExpr s
                        && s.getMemberValue() instanceof StringLiteralExpr v) {
                    sql = v.getValue();
                } else if (a instanceof com.github.javaparser.ast.expr.NormalAnnotationExpr n) {
                    for (MemberValuePair p : n.getPairs()) {
                        if ("value".equals(p.getNameAsString())
                                && p.getValue() instanceof StringLiteralExpr v2) {
                            sql = v2.getValue();
                        }
                    }
                }
                if (sql == null || sql.isBlank()) {
                    return;
                }
                result.getMagicHints().addAll(collectForKnownTables(sql, knownTables, sourceRef));
                result.getRelationHints().addAll(SqlLiteralExtractor.extractJoinRelations(sql, knownTables));
            });
        }
    }

    /** 常量名 → 列名（STATUS_SHIPPED → status；USER_TYPE_A → type） */
    static String snake(String constantName) {
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

    /** XML mapper：resultMap association/collection 关联 + select/insert/update 字面量与 JOIN */
    private void scanXml(SourceFile f, Collection<String> knownTables, ScanResult result, long deadline) throws Exception {
        String content = f.getContent();
        if (!content.contains("<mapper")) {
            return;
        }
        DocumentBuilderFactory dbf = DocumentBuilderFactory.newInstance();
        dbf.setFeature("http://apache.org/xml/features/nonvalidating/load-external-dtd", false);
        dbf.setNamespaceAware(false);
        Document doc = dbf.newDocumentBuilder()
                .parse(new ByteArrayInputStream(content.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        String sourceRef = f.getPath();

        // resultMap 关联声明（结构化，最可靠）
        NodeList maps = doc.getElementsByTagName("resultMap");
        for (int i = 0; i < maps.getLength(); i++) {
            Element rm = (Element) maps.item(i);
            String type = rm.getAttribute("type"); // 宿主 Java 类名（如 com.x.User）
            String childTable = toTableName(type);
            NodeList children = rm.getChildNodes();
            for (int k = 0; k < children.getLength(); k++) {
                if (!(children.item(k) instanceof Element tag)) {
                    continue;
                }
                String tag2 = tag.getTagName();
                if (!"association".equals(tag2) && !"collection".equals(tag2)) {
                    continue;
                }
                String column = tag.getAttribute("column");
                String pType = tag.getAttribute("javaType").isBlank()
                        ? tag.getAttribute("ofType") : tag.getAttribute("javaType");
                String parentTable = toTableName(pType);
                if (column.isBlank() || parentTable == null || childTable == null) {
                    continue;
                }
                if (!knownTables.contains(childTable) || !knownTables.contains(parentTable)) {
                    continue;
                }
                RelationHint h = new RelationHint(childTable, column, parentTable, "id",
                        RelationHint.SRC_ORM_JOIN, 0.9);
                h.getSourceRefs().add(sourceRef);
                result.getRelationHints().add(h);
            }
        }

        // SQL 文本节点：字面量 + JOIN（正则辅助）
        for (String tag : new String[]{"select", "insert", "update", "delete"}) {
            NodeList sqls = doc.getElementsByTagName(tag);
            for (int i = 0; i < sqls.getLength(); i++) {
                String sql = ((Element) sqls.item(i)).getTextContent();
                result.getMagicHints().addAll(
                        collectForKnownTables(sql, knownTables, sourceRef));
                result.getRelationHints().addAll(SqlLiteralExtractor.extractJoinRelations(sql, knownTables));
            }
        }
    }

    private List<MagicHint> collectForKnownTables(String sql, Collection<String> knownTables, String sourceRef) {
        List<MagicHint> hints = new ArrayList<>();
        for (String t : knownTables) {
            for (MagicHint h : SqlLiteralExtractor.extractLiterals(sql, t, sourceRef)) {
                if (!h.getValues().isEmpty()) {
                    hints.add(h);
                }
            }
        }
        return hints;
    }

    /** Java 类名/全限定名 → 表名（camelCase → snake_case） */
    static String toTableName(String javaType) {
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

