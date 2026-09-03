package com.perftestor.datagen.endpoint;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.expr.ArrayInitializerExpr;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.MemberValuePair;
import com.github.javaparser.ast.expr.NormalAnnotationExpr;
import com.github.javaparser.ast.expr.SingleMemberAnnotationExpr;
import com.github.javaparser.ast.expr.StringLiteralExpr;
import com.perftestor.datagen.model.EndpointRef;
import com.perftestor.datagen.model.SourceFile;
import com.perftestor.datagen.util.EndpointPath;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Optional;

/**
 * Controller 接口索引（JavaParser AST 实现）。
 *
 * <p>宿主已有正则实现 {@code EndpointScanner}，本类<b>不替换它</b>（压测中心选接口流程依赖其产出），
 * 而是提供方法级 AST 坐标，供「按接口分析调用链」精确定位入口方法。两者产出必须逐字段一致，
 * 由 {@code EndpointPathConsistencyTest} 守卫。</p>
 *
 * <p>一致性要点（实现时严格对齐正则实现）：</p>
 * <ul>
 *   <li>{@code className} 取 <b>simpleName（文件名去 .java）</b>，不是 FQCN</li>
 *   <li>路径经 {@link EndpointPath} 单点归一化，{@code {id}} 变量原样保留</li>
 *   <li>路径提取顺序：value/path 成员 → 注解内第一个字符串字面量（与正则兜底行为一致）</li>
 *   <li>{@code line} 取映射注解所在行</li>
 *   <li>单文件解析失败静默跳过，不中断整体扫描</li>
 * </ul>
 */
public final class EndpointIndexParser {

    private static final Logger log = LoggerFactory.getLogger(EndpointIndexParser.class);

    private static final String CONTROLLER = "@Controller";
    private static final String REST_CONTROLLER = "@RestController";
    private static final String REQUEST_MAPPING = "RequestMapping";
    /** 方法级映射注解名 → HTTP 方法（RequestMapping 需再判 method 属性） */
    private static final String[] MAPPINGS = {"GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping"};

    private final JavaParser parser = new JavaParser();

    /**
     * 解析全部 Controller 接口。
     *
     * @param javaFiles 仓库 Java 源码文件集合
     * @return 接口清单（未排序，顺序与入参文件顺序一致）
     */
    public List<EndpointRef> parse(List<SourceFile> javaFiles) {
        List<EndpointRef> out = new ArrayList<>();
        if (javaFiles == null) {
            return out;
        }
        for (SourceFile f : javaFiles) {
            if (!isJava(f) || !isController(f.getContent())) {
                continue;
            }
            try {
                parseFile(f, out);
            } catch (Exception e) {
                // 单文件失败静默跳过（防源码碎片化中断整体扫描）
                log.debug("skip controller file {}: {}", f.getPath(), e.getMessage());
            }
        }
        return out;
    }

    private void parseFile(SourceFile f, List<EndpointRef> out) {
        Optional<CompilationUnit> cuOpt = parser.parse(f.getContent()).getResult();
        if (cuOpt.isEmpty()) {
            return;
        }
        CompilationUnit cu = cuOpt.get();
        String className = EndpointPath.simpleNameOf(f.getPath());
        for (ClassOrInterfaceDeclaration type : cu.findAll(ClassOrInterfaceDeclaration.class)) {
            String classPrefix = EndpointPath.normalize(classMappingPath(type));
            for (MethodDeclaration m : type.getMethods()) {
                for (AnnotationExpr anno : m.getAnnotations()) {
                    String httpMethod = httpMethodOf(anno);
                    if (httpMethod == null) {
                        continue;
                    }
                    String path = EndpointPath.join(classPrefix, extractPath(anno));
                    EndpointRef ref = new EndpointRef(
                            httpMethod,
                            path,
                            className,
                            m.getNameAsString(),
                            f.getPath(),
                            anno.getBegin().map(p -> p.line).orElse(0));
                    out.add(ref);
                }
            }
        }
    }

    /** 类级 @RequestMapping 路径（无则空串） */
    private String classMappingPath(ClassOrInterfaceDeclaration type) {
        for (AnnotationExpr anno : type.getAnnotations()) {
            if (REQUEST_MAPPING.equals(anno.getNameAsString())) {
                return extractPath(anno);
            }
        }
        return "";
    }

    /**
     * 判定映射注解对应的 HTTP 方法。
     *
     * @return HTTP 方法大写；非映射注解返回 null
     */
    private String httpMethodOf(AnnotationExpr anno) {
        String name = anno.getNameAsString();
        for (String m : MAPPINGS) {
            if (m.equals(name)) {
                return m.substring(0, m.length() - "Mapping".length()).toUpperCase(Locale.ROOT);
            }
        }
        if (!REQUEST_MAPPING.equals(name)) {
            return null;
        }
        // @RequestMapping(method = RequestMethod.POST)：显式指定优先，缺省 GET（与正则实现一致）
        String text = anno.toString();
        int idx = text.toUpperCase(Locale.ROOT).indexOf("REQUESTMETHOD.");
        if (idx >= 0) {
            String tail = text.substring(idx + "REQUESTMETHOD.".length());
            for (String m : new String[]{"GET", "POST", "PUT", "DELETE", "PATCH"}) {
                if (tail.regionMatches(true, 0, m, 0, m.length())) {
                    return m;
                }
            }
        }
        return "GET";
    }

    /** 路径提取：value/path 成员优先，回退到注解内第一个字符串字面量 */
    private String extractPath(AnnotationExpr anno) {
        if (anno instanceof NormalAnnotationExpr n) {
            for (MemberValuePair p : n.getPairs()) {
                String key = p.getNameAsString();
                if ("value".equals(key) || "path".equals(key)) {
                    String v = firstString(p.getValue());
                    if (v != null) {
                        return v;
                    }
                }
            }
        } else if (anno instanceof SingleMemberAnnotationExpr s) {
            String v = firstString(s.getMemberValue());
            if (v != null) {
                return v;
            }
        }
        return firstStringIn(anno);
    }

    /** 取字符串字面量；数组取首元素（与正则实现对多值 path 的处理一致） */
    private static String firstString(Expression e) {
        if (e instanceof StringLiteralExpr s) {
            return s.getValue();
        }
        if (e instanceof ArrayInitializerExpr arr) {
            for (Expression item : arr.getValues()) {
                if (item instanceof StringLiteralExpr s) {
                    return s.getValue();
                }
            }
        }
        return null;
    }

    private static String firstStringIn(Node node) {
        return node.findFirst(StringLiteralExpr.class).map(StringLiteralExpr::getValue).orElse("");
    }

    private static boolean isJava(SourceFile f) {
        return f != null && f.getPath() != null && f.getPath().toLowerCase(Locale.ROOT).endsWith(".java");
    }

    private static boolean isController(String content) {
        return content != null && (content.contains(CONTROLLER) || content.contains(REST_CONTROLLER));
    }
}
