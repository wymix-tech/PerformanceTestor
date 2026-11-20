"""AST 接口索引等价单测：移植 EndpointIndexParserTest 全部 13 个用例。

方法级定位、路径归一化、className 语义。
"""

from ptde.analyzer.lang_java.endpoint_index_parser import EndpointIndexParser
from ptde.models.wire import SourceFile

CTRL = "src/main/java/com/demo/OrderController.java"

CONTROLLER = """
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
"""


def parse(content: str):
    return EndpointIndexParser().parse([SourceFile(CTRL, content)])


def keys(refs) -> set[str]:
    return {r.key() for r in refs}


def test_class_prefix_is_joined_with_method_path():
    assert "GET /api/order/list" in keys(parse(CONTROLLER))


def test_method_path_without_leading_slash_is_normalized():
    assert "GET /api/order/{id}" in keys(parse(CONTROLLER))


def test_path_variable_is_preserved():
    assert any(r.path == "/api/order/{id}" for r in parse(CONTROLLER)), \
        "{id} 路径变量必须原样保留，不得替换"


def test_trailing_slash_is_stripped():
    assert "DELETE /api/order/delete" in keys(parse(CONTROLLER)), \
        f"尾部斜杠必须去除，实际: {keys(parse(CONTROLLER))}"


def test_post_mapping_is_recognized():
    assert "POST /api/order/create" in keys(parse(CONTROLLER))


def test_request_mapping_method_attribute_is_honored():
    assert "PATCH /api/order/patch" in keys(parse(CONTROLLER))


def test_multi_value_path_takes_first():
    assert "GET /api/order/multi" in keys(parse(CONTROLLER)), \
        "多值 path 取首个（与正则实现一致）"


def test_mapping_without_path_falls_back_to_first_string_literal_like_regex_impl():
    # 正则实现的兜底是"注解内第一个字符串字面量"，AST 实现必须同行为，否则双实现不一致
    assert "GET /api/order/application/json" in keys(parse(CONTROLLER)), \
        f"实际: {keys(parse(CONTROLLER))}"


def test_class_name_is_simple_name_not_fqcn():
    refs = parse(CONTROLLER)
    assert {r.class_name for r in refs} == {"OrderController"}


def test_method_level_line_is_recorded():
    refs = parse(CONTROLLER)
    assert all(r.line > 0 for r in refs)
    assert refs[0].method_name == "list"


def test_non_controller_files_are_ignored():
    svc = "package com.demo; public class OrderService { public Object list() { return null; } }"
    assert parse(svc) == []


def test_broken_controller_does_not_throw():
    assert parse('@RestController public class X { @GetMapping("/a") ') == []


def test_controller_without_class_prefix_is_supported():
    c = """
    package com.demo;
    import org.springframework.web.bind.annotation.*;
    @RestController
    public class PingController {
        @GetMapping("/ping")
        public Object ping() { return null; }
    }
    """
    assert parse(c)[0].key() == "GET /ping"
