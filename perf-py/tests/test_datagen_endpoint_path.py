"""路径归一化单点工具等价单测：移植 EndpointPathTest 全部 9 个用例。"""

from datagen.endpoint_path import join, normalize, simple_name_of, strip_trailing_slash


def test_normalize_adds_leading_slash():
    assert normalize("order") == "/order"


def test_normalize_strips_trailing_slash():
    assert normalize("/order/") == "/order"
    assert normalize("/order///") == "/order"


def test_normalize_keeps_root():
    assert normalize("/") == "/"
    assert normalize("//") == "/"


def test_normalize_blank_to_empty():
    assert normalize(None) == ""
    assert normalize("   ") == ""


def test_join_concatenates_prefix_and_method_path():
    assert join("/api/order", "/list") == "/api/order/list"
    assert join("api/order", "list") == "/api/order/list"


def test_join_returns_root_when_both_blank():
    assert join("", "") == "/"
    assert join(None, None) == "/"


def test_join_keeps_path_variable_as_is():
    assert join("/api/order", "/{id}") == "/api/order/{id}"


def test_join_strips_trailing_slash_of_result():
    assert join("/api/order/", "/") == "/api/order"


def test_strip_trailing_slash_direct():
    assert strip_trailing_slash("/") == "/"
    assert strip_trailing_slash(None) == ""


def test_simple_name_of_java_file():
    assert simple_name_of("src/main/java/com/x/OrderController.java") == "OrderController"
    assert simple_name_of("OrderController.java") == "OrderController"
    assert simple_name_of("C:\\repo\\OrderController.java") == "OrderController"
    assert simple_name_of(None) == ""
