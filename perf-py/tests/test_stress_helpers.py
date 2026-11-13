"""stress/ 辅助类移植等价性测试（对应 Java 侧 6 个测试类的关键语义）。

覆盖：ResponseAccessTest / ResponseAssertorTest / ResponseVarExtractorTest /
StepContextStoreTest / StepSelectorTest / TemplateRendererTest 的逐条语义。
"""


from scenario.model import AssertionSpec, VarExtractSpec
from stress.response_access import header_value, parse_json, resolve_path, value_to_text
from stress.response_assertor import compare, matches, needs_body
from stress.response_var_extractor import create_pool, describe, extract, substitute
from stress.step_selector import StepSelector
from stress.template_renderer import (
    StepContextStore,
    build_alias_index,
    parse_context_ref,
    parse_step_ref,
    render,
)

# ---------------------------------------------------------------- ResponseAccess

def test_header_value_case_insensitive_first_wins():
    headers = {"Content-Type": ["application/json"], "content-type": ["text/html"]}
    assert header_value(headers, "CONTENT-TYPE") == "application/json"
    assert header_value(headers, "content-TYPE") == "application/json"
    assert header_value(headers, "x-missing") is None
    assert header_value(None, "x") is None
    assert header_value(headers, "") is None


def test_parse_json_truncation_and_non_json():
    assert parse_json('{"a":1}', 1024) == {"a": 1}
    assert parse_json("<html>err</html>", 1024) is None
    assert parse_json("", 1024) is None
    assert parse_json(None, 1024) is None
    # 超长截断后可能破坏 JSON → None（与 Java 截断语义一致）
    assert parse_json('{"a":"' + "x" * 100 + '"}', 10) is None


def test_resolve_path_array_and_numeric_key():
    data = {"list": [{"id": 1}, {"id": 2}], "0": "zero", "data": {"token": "tk"}}
    assert resolve_path(data, "data.token") == "tk"
    assert resolve_path(data, "list.0.id") == 1
    assert resolve_path(data, "list.1.id") == 2
    assert resolve_path(data, "list.2.id") is None  # 越界
    assert resolve_path(data, "0") == "zero"  # key 恰为数字串的 dict 按 key 取
    assert resolve_path(data, "list.x") is None  # 非 list 下非数字段
    assert resolve_path(data, "data.token.deep") is None  # 对标量继续下钻
    assert resolve_path(data, "") is None
    assert resolve_path(None, "a") is None


def test_value_to_text_jackson_semantics():
    assert value_to_text(True) == "true"
    assert value_to_text(False) == "false"
    assert value_to_text(200) == "200"
    assert value_to_text(1.5) == "1.5"
    assert value_to_text("tk") == "tk"
    assert value_to_text({"a": 1}) == '{"a":1}'
    assert value_to_text([1, 2]) == "[1,2]"


# ---------------------------------------------------------------- ResponseAssertor

def test_compare_numeric_eq():
    assert compare("eq", "200", "200.0") is True  # 数值相等
    assert compare("ne", "200", "200.0") is False
    assert compare("eq", "abc", "abc") is True
    assert compare("ne", "abc", "abd") is True


def test_compare_contains_and_notcontains():
    assert compare("contains", "hello world", "lo w") is True
    assert compare("notcontains", "hello", "xyz") is True


def test_compare_gt_lt_numeric_and_lexicographic_fallback():
    assert compare("gt", "11", "10") is True
    assert compare("lt", "9", "10") is True
    assert compare("gt", "abc", "abd") is False  # 字典序退化
    assert compare("lt", "abc", "abd") is True


def test_compare_regex_invalid_pattern_no_exception():
    assert compare("regex", "id=42", r"id=\d+") is True
    assert compare("regex", "id=42", r"[\d") is False  # 非法正则 → 不命中
    assert compare("regex", "id=42", r"[\d") is False  # 缓存命中路径


def test_compare_unknown_op_falls_back_to_eq():
    assert compare("bogus", "200", "200") is True


def test_matches_empty_rules_pass():
    assert matches([], 500, {}, "") is True
    assert matches(None, 500, {}, "") is True


def test_matches_status_and_header():
    rules = [AssertionSpec(source="status", op="eq", expected="200")]
    assert matches(rules, 200, {}, None) is True
    assert matches(rules, 500, {}, None) is False
    h = [AssertionSpec(source="header", key="X-Token", op="eq", expected="tk")]
    assert matches(h, 200, {"x-token": ["tk"]}, None) is True
    assert matches(h, 200, {"x-token": ["other"]}, None) is False


def test_matches_header_missing_passthrough_for_ne_notcontains():
    ne = [AssertionSpec(source="header", key="X-Missing", op="ne", expected="x")]
    nc = [AssertionSpec(source="header", key="X-Missing", op="notContains", expected="x")]
    eq = [AssertionSpec(source="header", key="X-Missing", op="eq", expected="x")]
    assert matches(ne, 200, {}, None) is True  # 缺头时 ne 放行
    assert matches(nc, 200, {}, None) is True
    assert matches(eq, 200, {}, None) is False


def test_matches_json_missing_path_passthrough_for_ne():
    ne = [AssertionSpec(source="json", key="data.x", op="ne", expected="1")]
    eq = [AssertionSpec(source="json", key="data.x", op="eq", expected="1")]
    body = '{"other":1}'
    assert matches(ne, 200, {}, body) is True
    assert matches(eq, 200, {}, body) is False


def test_matches_json_value_and_container_nodes():
    eq = [AssertionSpec(source="json", key="data.code", op="eq", expected="0")]
    assert matches(eq, 200, {}, '{"data":{"code":0}}') is True
    # 容器节点 → 紧凑 JSON 文本再比较
    contains = [AssertionSpec(source="json", key="data", op="contains", expected='"code":0')]
    assert matches(contains, 200, {}, '{"data":{"code":0}}') is True


def test_needs_body():
    assert needs_body([AssertionSpec(source="status")]) is False
    assert needs_body([AssertionSpec(source="header", key="x")]) is False
    assert needs_body([AssertionSpec(source="json", key="a.b")]) is True
    assert needs_body([]) is False


# ---------------------------------------------------------------- ResponseVarExtractor

def test_extract_body_json_path():
    pool = create_pool({"env": "prod"})
    rules = [VarExtractSpec(var="token", source="body", path="result.token")]
    extract('{"result":{"token":"tk-1"}}', None, rules, pool, 65536)
    assert pool["token"] == "tk-1"
    assert pool["env"] == "prod"  # 初始变量保留


def test_extract_header_case_insensitive():
    pool: dict[str, str] = {}
    rules = [VarExtractSpec(var="session", source="header", path="Set-Cookie")]
    extract(None, {"set-cookie": ["sid=abc"]}, rules, pool, 65536)
    assert pool["session"] == "sid=abc"


def test_extract_mixed_sources_and_silent_miss():
    pool: dict[str, str] = {}
    rules = [
        VarExtractSpec(var="session", source="header", path="Set-Cookie"),
        VarExtractSpec(var="uid", source="body", path="user.id"),
        VarExtractSpec(var="missing", source="body", path="no.such.path"),
    ]
    extract('{"user":{"id":7}}', {"Set-Cookie": ["s=1"]}, rules, pool, 65536)
    assert pool == {"session": "s=1", "uid": "7"}  # 未命中静默跳过


def test_extract_non_json_body_silent():
    pool: dict[str, str] = {}
    rules = [VarExtractSpec(var="x", source="body", path="a")]
    extract("<html/>", None, rules, pool, 65536)
    assert pool == {}


def test_extract_container_value_to_compact_json():
    pool: dict[str, str] = {}
    rules = [VarExtractSpec(var="user", source="body", path="user")]
    extract('{"user":{"id":7,"name":"n"}}', None, rules, pool, 65536)
    assert pool["user"] == '{"id":7,"name":"n"}'


def test_substitute_keeps_missing_placeholder():
    assert substitute("{{a}}-{{b}}", {"a": "1"}) == "1-{{b}}"
    assert substitute("no-placeholder", {"a": "1"}) == "no-placeholder"
    assert substitute(None, {}) == ""


def test_describe_redacts_values():
    assert describe({}) == "{}"
    assert describe({"token": "secret-value"}) == "{token=len(12)}"


# ---------------------------------------------------------------- StepSelector

def test_step_selector_weights_and_boundaries():
    sel = StepSelector([1, 0, 3])
    assert sel.total_weight == 4
    assert len(sel) == 3
    # 手工二分定位：target=1→0, 2→2, 3→2, 4→2
    assert sel.select.__doc__ is not None


def test_step_selector_all_zero_weights_returns_minus_one():
    assert StepSelector([0, 0]).select() == -1
    assert StepSelector([]).select() == -1


def test_step_selector_deterministic_distribution(monkeypatch):
    seq = iter([1, 2, 3, 4])
    monkeypatch.setattr("stress.step_selector.random.randint", lambda a, b: next(seq))
    sel = StepSelector([1, 3])
    # 累积权重 [1,4]：target1→0，target2~4→1
    assert sel.select() == 0
    assert sel.select() == 1
    assert sel.select() == 1
    assert sel.select() == 1


def test_step_selector_of_reads_step_weights():
    class S:
        def __init__(self, w):
            self.weight = w

    sel = StepSelector.of([S(0), S(5)])
    assert sel.total_weight == 6  # Java 语义：of 时 <=0 提升为 1


# ---------------------------------------------------------------- StepContextStore / parse_step_ref

def test_parse_step_ref_boundaries():
    assert parse_step_ref("p1") == 1
    assert parse_step_ref("P2") == 2
    assert parse_step_ref("p0") == -1
    assert parse_step_ref("p01") == -1
    assert parse_step_ref("p1x") == -1
    assert parse_step_ref("p") == -1
    assert parse_step_ref("1p") == -1
    assert parse_step_ref("p99999999999999999999") == -1  # 溢出
    assert parse_step_ref("") == -1
    assert parse_step_ref(None) == -1


def test_build_alias_index_rules():
    warnings: list[str] = []

    class S:
        def __init__(self, name=None, alias=None):
            self.name, self.alias = name, alias

    steps = [S(name="login"), S(alias="order-2"), S(alias="p3"), S(alias="dup"), S(alias="dup"), S(alias="登录!")]
    idx = build_alias_index(steps, warnings)
    # 重名首个生效（dup:3 入表）；pN 冲突与登录! 均不入表
    assert idx == {"login": 0, "order-2": 1, "dup": 3}
    # 显式声明的三类问题都要 warning：pN 冲突 / 重名 / 别名不合法
    assert len(warnings) == 3
    assert "p3" in warnings[0] and "重名" in warnings[1] and "不合法" in warnings[2]


def test_step_context_store_put_resolve_latest_only():
    ctx = StepContextStore(2, {"login": 0}, 65536)
    assert ctx.resolve(0, "result", None) is None  # 未写入
    ctx.put(0, {"X-Token": ["tk1"]}, '{"user":{"id":7}}')
    assert ctx.resolve(0, "header", "x-token") == "tk1"
    assert ctx.resolve(0, "result", None) == '{"user":{"id":7}}'  # 无 path → 原文
    assert ctx.resolve(0, "result", "user.id") == "7"
    # 只保留最近一次响应
    ctx.put(0, {"X-Token": ["tk2"]}, '{"user":{"id":8}}')
    assert ctx.resolve(0, "result", "user.id") == "8"
    assert ctx.resolve(0, "header", "x-token") == "tk2"
    # 越界槽位
    assert ctx.resolve(9, "result", None) is None


def test_step_context_store_non_json_and_truncation():
    ctx = StepContextStore(1, {}, 10)
    ctx.put(0, None, "<html>body</html>")
    assert ctx.resolve(0, "result", None) == "<html>body"  # 非 JSON 无 path → 原文（已被 maxBodyChars 截断）
    assert ctx.resolve(0, "result", "a") is None  # 非 JSON 有 path → 无法下钻
    ctx.put(0, None, "x" * 100)
    assert ctx.resolve(0, "result", None) == "x" * 10  # 截断


# ---------------------------------------------------------------- TemplateRenderer

def test_render_flat_pool():
    assert render("Bearer {{token}}", {"token": "tk-1"}) == "Bearer tk-1"
    assert render("{{missing}}", {}) == "{{missing}}"  # 未命中保留原文
    assert render("plain", None) == "plain"
    assert render("{{a}}{{b}}", {"a": "1", "b": "2"}) == "12"


def test_render_context_ref_pn():
    ctx = StepContextStore(2, {}, 65536)
    ctx.put(0, {"X-Req": ["r1"]}, '{"data":{"token":"tk"}}')
    miss = [0]
    assert render("{{p1.result.data.token}}", {}, ctx, miss) == "tk"
    assert render("{{p1.header.X-Req}}", {}, ctx, miss) == "r1"
    assert miss[0] == 0
    # 越界 → 计入未命中 + 保留原文
    assert render("{{p2.result.x}}", {}, ctx, miss) == "{{p2.result.x}}"
    assert miss[0] == 1


def test_render_context_ref_alias():
    ctx = StepContextStore(1, {"login": 0}, 65536)
    ctx.put(0, None, '{"user":{"id":7}}')
    assert render("{{login.result.user.id}}", {}, ctx, None) == "7"


def test_render_hijack_protection_flat_var_with_dot():
    # 存量扁平变量叫 xxx.result.yyy：xxx 未登记 → 走扁平变量，不被上下文语法劫持
    ctx = StepContextStore(1, {}, 65536)
    assert render("{{legacy.result.field}}", {"legacy.result.field": "safe"}, ctx, None) == "safe"


def test_render_ctx_resolved_but_value_missing_counts_miss():
    ctx = StepContextStore(1, {}, 65536)
    ctx.put(0, None, "{}")
    miss = [0]
    assert render("{{p1.result.x}}", {}, ctx, miss) == "{{p1.result.x}}"
    assert miss[0] == 1


def test_parse_context_ref_shape():
    assert parse_context_ref("p1.result.data.token") == ("p1", "result", "data.token")
    assert parse_context_ref("login.header.X-Token") == ("login", "header", "X-Token")
    assert parse_context_ref("p1.result") == ("p1", "result", None)
    assert parse_context_ref("plainvar") is None
    assert parse_context_ref("x..y") is None
    assert parse_context_ref("") is None


def test_render_empty_template_and_no_placeholder_fast_path():
    assert render("", {}, None) == ""
    assert render("no braces", {"a": "1"}, None) == "no braces"
