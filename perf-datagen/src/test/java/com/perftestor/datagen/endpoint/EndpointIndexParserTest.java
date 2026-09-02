package com.perftestor.datagen.endpoint;

import com.perftestor.datagen.model.EndpointRef;
import com.perftestor.datagen.model.SourceFile;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** AST 接口索引单测：方法级定位、路径归一化、className 语义。 */
class EndpointIndexParserTest {

    private static final String CTRL = "src/main/java/com/demo/OrderController.java";

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

                @RequestMapping(value = {"/multi", "/alias"})
                public Object multi() {
                    return null;
                }

                @GetMapping(produces = "application/json")
                public Object noPath() {
                    return null;
                }
            }
            """;

    private static List<EndpointRef> parse(String content) {
        return new EndpointIndexParser().parse(List.of(new SourceFile(CTRL, content)));
    }

    private static Set<String> keys(List<EndpointRef> refs) {
        return refs.stream().map(EndpointRef::key).collect(Collectors.toSet());
    }

    @Test
    void classPrefixIsJoinedWithMethodPath() {
        assertTrue(keys(parse(CONTROLLER)).contains("GET /api/order/list"));
    }

    @Test
    void methodPathWithoutLeadingSlashIsNormalized() {
        assertTrue(keys(parse(CONTROLLER)).contains("GET /api/order/{id}"));
    }

    @Test
    void pathVariableIsPreserved() {
        assertTrue(parse(CONTROLLER).stream().anyMatch(r -> "/api/order/{id}".equals(r.getPath())),
                "{id} 路径变量必须原样保留，不得替换");
    }

    @Test
    void trailingSlashIsStripped() {
        assertTrue(keys(parse(CONTROLLER)).contains("DELETE /api/order/delete"),
                "尾部斜杠必须去除，实际: " + keys(parse(CONTROLLER)));
    }

    @Test
    void postMappingIsRecognized() {
        assertTrue(keys(parse(CONTROLLER)).contains("POST /api/order/create"));
    }

    @Test
    void requestMappingMethodAttributeIsHonored() {
        assertTrue(keys(parse(CONTROLLER)).contains("PATCH /api/order/patch"));
    }

    @Test
    void multiValuePathTakesFirst() {
        assertTrue(keys(parse(CONTROLLER)).contains("GET /api/order/multi"),
                "多值 path 取首个（与正则实现一致）");
    }

    @Test
    void mappingWithoutPathFallsBackToFirstStringLiteralLikeRegexImpl() {
        // 正则实现的兜底是"注解内第一个字符串字面量"，AST 实现必须同行为，否则双实现不一致
        assertTrue(keys(parse(CONTROLLER)).contains("GET /api/order/application/json"),
                "实际: " + keys(parse(CONTROLLER)));
    }

    @Test
    void classNameIsSimpleNameNotFqcn() {
        List<EndpointRef> refs = parse(CONTROLLER);
        assertEquals(Set.of("OrderController"), refs.stream().map(EndpointRef::getClassName).collect(Collectors.toSet()));
    }

    @Test
    void methodLevelLineIsRecorded() {
        List<EndpointRef> refs = parse(CONTROLLER);
        assertTrue(refs.stream().allMatch(r -> r.getLine() > 0));
        assertEquals("list", refs.get(0).getMethodName());
    }

    @Test
    void nonControllerFilesAreIgnored() {
        String svc = "package com.demo; public class OrderService { public Object list() { return null; } }";
        assertTrue(parse(svc).isEmpty());
    }

    @Test
    void brokenControllerDoesNotThrow() {
        assertTrue(parse("@RestController public class X { @GetMapping(\"/a\") ").isEmpty());
    }

    @Test
    void controllerWithoutClassPrefixIsSupported() {
        String c = """
                package com.demo;
                import org.springframework.web.bind.annotation.*;
                @RestController
                public class PingController {
                    @GetMapping("/ping")
                    public Object ping() { return null; }
                }
                """;
        assertEquals("GET /ping", parse(c).get(0).key());
    }
}
