package com.perftestor.datagen.endpoint;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.body.VariableDeclarator;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.expr.ThisExpr;
import com.perftestor.datagen.model.EndpointRef;
import com.perftestor.datagen.model.SourceFile;
import com.perftestor.datagen.util.EndpointPath;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 调用链分析器：从 Controller 入口方法出发按需展开（BFS），终止于 Mapper 调用。
 *
 * <p>不做全仓库建图——全量建图在大仓库上是分钟级。只解析「被调目标的声明类型」所在文件，
 * 逐层展开，并受 {@value #MAX_DEPTH} 深度 / {@value #MAX_FILES} 文件数 /
 * {@value #MAX_NODES} 节点数 / {@value #TIMEOUT_MS}ms 总超时四重熔断保护。</p>
 *
 * <p>无状态、线程安全：AST 缓存为单次分析请求的局部对象，分析结束即可回收。</p>
 */
public final class CallGraphAnalyzer {

    private static final Logger log = LoggerFactory.getLogger(CallGraphAnalyzer.class);

    /** 展开深度上限（Controller → Service → 子 Service → Mapper 足够） */
    public static final int MAX_DEPTH = 4;
    /** 参与分析的文件数上限 */
    public static final int MAX_FILES = 300;
    /** BFS 节点数上限 */
    public static final int MAX_NODES = 200_000;
    /** 分析总超时（ms） */
    public static final long TIMEOUT_MS = 15_000;
    /** 触发熔断后已得结论的置信度 */
    public static final double TRIPPED_CONFIDENCE = 0.4;
    /** 接口存在多个实现时结论的置信度 */
    public static final double MULTI_IMPL_CONFIDENCE = 0.6;

    private static final String[] MAPPER_SUFFIX = {"Mapper", "Dao", "Repository"};

    private final JavaParser parser = new JavaParser();

    /** Mapper 调用点：一个接口方法对 Mapper 的一次调用 */
    public static final class MapperInvocation {
        private final String mapperType;
        private final String methodName;
        private final String callSite;
        private final String mapperFile;

        MapperInvocation(String mapperType, String methodName, String callSite, String mapperFile) {
            this.mapperType = mapperType;
            this.methodName = methodName;
            this.callSite = callSite;
            this.mapperFile = mapperFile;
        }

        /** Mapper 简单类型名（如 OrderMapper） */
        public String getMapperType() {
            return mapperType;
        }

        public String getMethodName() {
            return methodName;
        }

        /** 调用点证据：文件:行号 */
        public String getCallSite() {
            return callSite;
        }

        /** Mapper 源码文件相对路径（未定位到文件时为空） */
        public String getMapperFile() {
            return mapperFile == null ? "" : mapperFile;
        }

        @Override
        public String toString() {
            return mapperType + "#" + methodName + " @ " + callSite;
        }
    }

    /** 分析结果 */
    public static final class Result {
        private final List<MapperInvocation> invocations = new ArrayList<>();
        private final Set<String> unresolved = new LinkedHashSet<>();
        private boolean tripped;
        private boolean multiImpl;

        public List<MapperInvocation> getInvocations() {
            return invocations;
        }

        /** 未解析原因集合（去重，直接展示给用户） */
        public Set<String> getUnresolved() {
            return unresolved;
        }

        /** 是否触发熔断（深度/文件/节点/超时任一） */
        public boolean isTripped() {
            return tripped;
        }

        /** 是否命中「接口多实现」（并集纳入，结论置信度下降） */
        public boolean isMultiImpl() {
            return multiImpl;
        }

        void trip(String reason) {
            this.tripped = true;
            this.unresolved.add(reason);
        }
    }

    /** BFS 节点 */
    private static final class Node {
        private final String file;
        private final String type;
        private final String method;
        private final int depth;

        Node(String file, String type, String method, int depth) {
            this.file = file;
            this.type = type;
            this.method = method;
            this.depth = depth;
        }

        String key() {
            return file + "#" + type + "#" + method;
        }
    }

    // ===== 单次分析的局部状态 =====

    private final Map<String, String> contentByPath = new HashMap<>();
    private final Map<String, List<String>> filesByType = new HashMap<>();
    private final Map<String, Optional<CompilationUnit>> astCache = new ConcurrentHashMap<>();
    private final long deadline;
    private int parsedFiles;

    public CallGraphAnalyzer() {
        this(System.currentTimeMillis() + TIMEOUT_MS);
    }

    /** 测试可见：允许注入固定 deadline 以验证熔断 */
    CallGraphAnalyzer(long deadline) {
        this.deadline = deadline;
    }

    /**
     * 从入口接口方法出发展开调用链。
     *
     * @param javaFiles 仓库 Java 源码（用于类型 → 文件解析）
     * @param entry     入口接口（AST 索引产出）
     * @return Mapper 调用点与未解析原因；不会抛异常
     */
    public Result analyze(List<SourceFile> javaFiles, EndpointRef entry) {
        Result result = new Result();
        if (javaFiles == null || entry == null || entry.getDeclaringFile() == null) {
            result.unresolved.add("入口接口信息不完整，无法分析调用链");
            return result;
        }
        index(javaFiles);

        Deque<Node> queue = new ArrayDeque<>();
        Set<String> visited = new java.util.HashSet<>();
        Node root = new Node(entry.getDeclaringFile(), EndpointPath.simpleNameOf(entry.getDeclaringFile()),
                entry.getMethodName(), 0);
        queue.add(root);
        visited.add(root.key());

        int nodes = 0;
        while (!queue.isEmpty()) {
            if (System.currentTimeMillis() > deadline) {
                result.trip("调用链分析超时（>" + TIMEOUT_MS + "ms），结论可能不完整");
                break;
            }
            if (++nodes > MAX_NODES || parsedFiles > MAX_FILES) {
                result.trip("调用链规模超上限（节点 " + MAX_NODES + " / 文件 " + MAX_FILES + "），结论可能不完整");
                break;
            }
            Node node = queue.poll();
            if (node.depth > MAX_DEPTH) {
                result.trip("调用链深度超上限（>" + MAX_DEPTH + " 层），深层调用未展开");
                continue;
            }
            Optional<CompilationUnit> cuOpt = ast(node.file);
            if (cuOpt.isEmpty()) {
                result.unresolved.add("源码解析失败或未索引: " + node.file);
                continue;
            }
            CompilationUnit cu = cuOpt.get();
            Optional<ClassOrInterfaceDeclaration> typeOpt = findType(cu, node.type);
            if (typeOpt.isEmpty()) {
                result.unresolved.add("未找到类型声明: " + node.type + " (" + node.file + ")");
                continue;
            }
            ClassOrInterfaceDeclaration type = typeOpt.get();
            Optional<MethodDeclaration> methodOpt = type.getMethods().stream()
                    .filter(m -> m.getNameAsString().equals(node.method)).findFirst();
            if (methodOpt.isEmpty()) {
                // 同名重载/继承方法：退化为整类型扫描，仍可能命中 Mapper 调用
                result.unresolved.add("未定位到方法体，按类型整体扫描: " + node.type + "#" + node.method);
                for (MethodDeclaration m : type.getMethods()) {
                    walk(cu, type, m, node, queue, visited, result);
                }
                continue;
            }
            walk(cu, type, methodOpt.get(), node, queue, visited, result);
        }
        log.debug("call graph done: entry={} invocations={} unresolved={} tripped={}",
                entry, result.getInvocations().size(), result.getUnresolved().size(), result.isTripped());
        return result;
    }

    // ===== 遍历 =====

    private void walk(CompilationUnit cu, ClassOrInterfaceDeclaration type, MethodDeclaration method,
                      Node node, Deque<Node> queue, Set<String> visited, Result result) {
        for (MethodCallExpr call : method.findAll(MethodCallExpr.class)) {
            String callee = call.getNameAsString();
            Optional<Expression> scope = call.getScope();
            String targetType;
            if (scope.isEmpty()) {
                targetType = node.type;                                   // this.xxx() / 本类方法
            } else if (scope.get() instanceof ThisExpr
                    || (scope.get() instanceof FieldAccessExpr fa && fa.getScope() instanceof ThisExpr)) {
                targetType = node.type;
            } else if (scope.get() instanceof NameExpr ne) {
                targetType = resolveIdentifierType(cu, type, method, ne.getNameAsString());
            } else if (scope.get() instanceof FieldAccessExpr fa) {
                targetType = resolveIdentifierType(cu, type, method, fa.getNameAsString());
            } else {
                targetType = null;
            }

            if (targetType == null) {
                // 无法静态确定的调用：JDK 静态工具（Math/UUID）、容器方法（List#size）、
                // var 推导等。与数据库访问无关，静默跳过——
                // unresolved 通道要保持干净，否则真正的「未覆盖」会被噪声淹没
                continue;
            }
            if (isMapperType(targetType)) {
                result.getInvocations().add(new MapperInvocation(targetType, callee, sourceRef(node.file, call),
                        firstFileOfType(targetType)));
                continue;
            }
            if (node.depth + 1 > MAX_DEPTH) {
                result.trip("调用链深度超上限（>" + MAX_DEPTH + " 层），深层调用未展开");
                continue;
            }
            List<String> implFiles = resolveImplementationFiles(targetType);
            if (implFiles.isEmpty()) {
                // 只对「可能含业务逻辑」的类型报未覆盖；List/Map/String/Optional 等
                // 容器与 JDK 类型没有源码是常态，报出来只会淹没有效信息
                if (looksLikeBusinessType(targetType)) {
                    result.unresolved.add("未找到类型源码，无法继续展开: " + targetType + "（" + node.file + "）");
                }
                continue;
            }
            if (implFiles.size() > 1) {
                result.multiImpl = true;
            }
            for (String implFile : implFiles) {
                Node next = new Node(implFile, targetType, callee, node.depth + 1);
                if (visited.add(next.key())) {
                    queue.add(next);
                }
            }
        }
    }

    /**
     * 解析被调用者的声明类型：字段注入类型 → 方法参数类型 → 局部变量初始化类型。
     * 返回简单类型名；无法解析返回 null。
     */
    private String resolveIdentifierType(CompilationUnit cu, ClassOrInterfaceDeclaration type,
                                         MethodDeclaration method, String name) {
        // 1. 字段注入（@Autowired/@Resource 字段，含构造器注入的 final 字段）
        for (FieldDeclaration fd : type.getFields()) {
            for (VariableDeclarator vd : fd.getVariables()) {
                if (vd.getNameAsString().equals(name)) {
                    return simpleTypeName(vd.getTypeAsString());
                }
            }
        }
        // 2. 方法参数
        for (Parameter p : method.getParameters()) {
            if (p.getNameAsString().equals(name)) {
                return simpleTypeName(p.getTypeAsString());
            }
        }
        // 3. 局部变量：有显式声明类型时直接用（List<User> → List，无对应源码时会安静跳过）
        for (VariableDeclarator vd : method.findAll(VariableDeclarator.class)) {
            if (!vd.getNameAsString().equals(name)) {
                continue;
            }
            if (vd.getInitializer().isPresent() && vd.getInitializer().get() instanceof ObjectCreationExpr oc) {
                return simpleTypeName(oc.getTypeAsString());
            }
            return "var".equals(vd.getTypeAsString()) ? null : simpleTypeName(vd.getTypeAsString());
        }
        // 4. 同文件其他类型（内部类/父类字段）兜底
        for (FieldDeclaration fd : cu.findAll(FieldDeclaration.class)) {
            for (VariableDeclarator vd : fd.getVariables()) {
                if (vd.getNameAsString().equals(name)) {
                    return simpleTypeName(vd.getTypeAsString());
                }
            }
        }
        return null;
    }

    /**
     * 目标类型对应的实现文件：若目标是接口则纳入全部实现类（并集，避免漏表）；
     * 否则按「文件名 = 类型名」的 Java 惯例定位。
     */
    private List<String> resolveImplementationFiles(String targetType) {
        List<String> direct = filesByType.get(targetType);
        if (direct == null || direct.isEmpty()) {
            return List.of();
        }
        boolean isInterface = false;
        for (String f : direct) {
            Optional<CompilationUnit> c = ast(f);
            if (c.isPresent() && findType(c.get(), targetType).map(ClassOrInterfaceDeclaration::isInterface).orElse(false)) {
                isInterface = true;
                break;
            }
        }
        if (!isInterface) {
            return direct;
        }
        List<String> impls = new ArrayList<>();
        String marker = "implements";
        for (Map.Entry<String, String> e : contentByPath.entrySet()) {
            String content = e.getValue();
            int i = content.indexOf(marker);
            while (i >= 0) {
                String tail = content.substring(i + marker.length()).trim();
                if (tail.startsWith(targetType) && !Character.isLetterOrDigit(
                        tail.length() > targetType.length() ? tail.charAt(targetType.length()) : ' ')) {
                    impls.add(e.getKey());
                    break;
                }
                i = content.indexOf(marker, i + marker.length());
            }
        }
        return impls.isEmpty() ? direct : impls;
    }

    /** 可能包含业务逻辑、值得继续展开的类型后缀（用于过滤容器/JDK 类型的噪声告警） */
    private static final String[] BUSINESS_SUFFIX = {
            "Service", "ServiceImpl", "Manager", "ManagerImpl", "Mapper", "Dao", "Repository",
            "Client", "Helper", "Handler", "Provider", "Processor", "BO", "Biz", "Facade"
    };

    /** 是否为疑似业务类型（无源码时值得提示用户） */
    private static boolean looksLikeBusinessType(String type) {
        if (type == null) {
            return false;
        }
        for (String suffix : BUSINESS_SUFFIX) {
            if (type.endsWith(suffix)) {
                return true;
            }
        }
        return false;
    }

    private boolean isMapperType(String type) {
        if (type == null) {
            return false;
        }
        for (String suffix : MAPPER_SUFFIX) {
            if (type.endsWith(suffix)) {
                return true;
            }
        }
        return false;
    }

    private String firstFileOfType(String type) {
        List<String> files = filesByType.get(type);
        return files == null || files.isEmpty() ? "" : files.get(0);
    }

    private static String simpleTypeName(String rawType) {
        if (rawType == null) {
            return null;
        }
        String s = rawType;
        int generic = s.indexOf('<');
        if (generic >= 0) {
            s = s.substring(0, generic); // List<Order> → List（容器类型无需展开）
        }
        int dot = s.lastIndexOf('.');
        if (dot >= 0) {
            s = s.substring(dot + 1);
        }
        s = s.replace("[]", "").trim();
        return s.isEmpty() ? null : s;
    }

    private static String sourceRef(String file, com.github.javaparser.ast.Node n) {
        return file + ":" + n.getBegin().map(p -> p.line).orElse(0);
    }

    private static Optional<ClassOrInterfaceDeclaration> findType(CompilationUnit cu, String simpleName) {
        for (ClassOrInterfaceDeclaration t : cu.findAll(ClassOrInterfaceDeclaration.class)) {
            if (simpleName.equals(t.getNameAsString())) {
                return Optional.of(t);
            }
        }
        return cu.findAll(ClassOrInterfaceDeclaration.class).stream().findFirst();
    }

    // ===== 索引与缓存 =====

    private void index(List<SourceFile> files) {
        for (SourceFile f : files) {
            String path = f.getPath();
            if (path == null || !path.toLowerCase(Locale.ROOT).endsWith(".java") || f.getContent() == null) {
                continue;
            }
            contentByPath.put(path, f.getContent());
            filesByType.computeIfAbsent(EndpointPath.simpleNameOf(path), k -> new ArrayList<>()).add(path);
        }
    }

    /** 惰性解析 + 缓存；解析失败记为空 Optional，避免重复尝试 */
    private Optional<CompilationUnit> ast(String file) {
        return astCache.computeIfAbsent(file, f -> {
            parsedFiles++;
            String content = contentByPath.get(f);
            if (content == null) {
                return Optional.empty();
            }
            try {
                return parser.parse(content).getResult();
            } catch (Exception e) {
                log.debug("parse failed: {}", f);
                return Optional.empty();
            }
        });
    }
}
