package com.perfserver.source;

import com.perfserver.entity.SourceFile;
import com.perftestor.datagen.endpoint.EndpointIndexParser;
import com.perftestor.datagen.model.EndpointRef;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;

/**
 * 双解析器一致性守卫。
 *
 * <p>宿主存在两条接口解析链路：正则实现 {@link EndpointScanner}（压测中心选接口流程在用）
 * 与 AST 实现 {@link EndpointIndexParser}（按接口分析调用链在用）。两者一旦在路径归一化、
 * HTTP 方法判定或 className 语义上出现分歧，{@code (httpMethod, path)} 精确匹配会静默降级，
 * 且极难定位。本测试对同一批 Controller 源码跑双实现，逐条断言三者一致。</p>
 */
class EndpointPathConsistencyTest {

    private static final String CTRL = "src/main/java/com/demo/OrderController.java";
    private static final String CTRL_NO_PREFIX = "src/main/java/com/demo/PingController.java";
    private static final String CTRL_WEIRD = "src/main/java/com/demo/WeirdController.java";

    /** 常规写法：类前缀 + 各方法级映射 */
    private static final String CONTROLLER = """
            package com.demo;

            import org.springframework.web.bind.annotation.*;

            @RestController
            @RequestMapping("/api/order")
            public class OrderController {

                @GetMapping("/list")
                public Object list() {
                    return null;
                }

                @GetMapping("{id}")
                public Object detail() {
                    return null;
                }

                @PostMapping("/create")
                public Object create() {
                    return null;
                }

                @DeleteMapping("/delete/")
                public Object delete() {
                    return null;
                }

                @RequestMapping(value = "/patch", method = RequestMethod.PATCH)
                public Object patch() {
                    return null;
                }
            }
            """;

    /** 无类前缀 */
    private static final String NO_PREFIX = """
            package com.demo;

            import org.springframework.web.bind.annotation.*;

            @RestController
            public class PingController {

                @GetMapping("/ping")
                public Object ping() {
                    return null;
                }
            }
            """;

    /** 边界写法：多值 path、无路径注解、类前缀带尾斜杠 */
    private static final String WEIRD = """
            package com.demo;

            import org.springframework.web.bind.annotation.*;

            @RestController
            @RequestMapping("/api/weird/")
            public class WeirdController {

                @GetMapping(value = {"/multi", "/alias"})
                public Object multi() {
                    return null;
                }

                @GetMapping(produces = "application/json")
                public Object noPath() {
                    return null;
                }

                @RequestMapping
                public Object bare() {
                    return null;
                }
            }
            """;

    @Test
    void bothImplementationsAgreeOnRegularController() {
        assertConsistent(CTRL, CONTROLLER);
    }

    @Test
    void bothImplementationsAgreeOnControllerWithoutClassPrefix() {
        assertConsistent(CTRL_NO_PREFIX, NO_PREFIX);
    }

    @Test
    void bothImplementationsAgreeOnWeirdMappings() {
        assertConsistent(CTRL_WEIRD, WEIRD);
    }

    private void assertConsistent(String path, String content) {
        Set<String> regex = regexKeys(path, content);
        Set<String> ast = astKeys(path, content);
        assertFalse(regex.isEmpty(), "正则实现未解析出接口，用例无效: " + path);
        assertEquals(regex, ast, "双实现产出不一致（" + path + "）\n正则: " + regex + "\nAST : " + ast);
    }

    /** 正则实现：直接复用 EndpointScanner 的单文件扫描逻辑 */
    private static Set<String> regexKeys(String path, String content) {
        SourceFile sf = new SourceFile();
        sf.setRelativePath(path);
        sf.setFileName(path.substring(path.lastIndexOf('/') + 1));
        List<Map<String, Object>> out = new ArrayList<>();
        EndpointScanner.scanFile(sf, content, out);
        return out.stream()
                .map(m -> key(m.get("httpMethod"), m.get("path"), m.get("className")))
                .collect(Collectors.toCollection(LinkedHashSet::new));
    }

    /** AST 实现：EndpointIndexParser */
    private static Set<String> astKeys(String path, String content) {
        List<EndpointRef> refs = new EndpointIndexParser()
                .parse(List.of(new com.perftestor.datagen.model.SourceFile(path, content)));
        return refs.stream()
                .map(r -> key(r.getHttpMethod(), r.getPath(), r.getClassName()))
                .collect(Collectors.toCollection(LinkedHashSet::new));
    }

    private static String key(Object httpMethod, Object path, Object className) {
        return httpMethod + " " + path + " @" + className;
    }
}
