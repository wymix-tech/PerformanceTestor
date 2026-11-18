"""Java 源码 AST 适配层：tree-sitter-java 实现（替换 JavaParser，Issue #23 阶段 3 步骤 4d）。

仅暴露 datagen 各解析器（ORM/endpoint/调用链）实际用到的那部分 JavaParser 能力：
findAll / 注解读写 / 字符串与整数字面量 / 字段与方法声明 / 方法调用与作用域。

等价性要点（与 JavaParser 语义逐条对齐）：
- 语法错误 → 返回 None（对齐 JavaParser ParseResult.isSuccessful=false 分支）；
- 字符串字面量取引号内原文、转义不展开（对齐 StringLiteralExpr.getValue）；
- 整数字面量仅纯十进制数字可解析（Long.parseLong 等价：L 后缀/下划线/十六进制
  均解析失败，调用方按「无初始值」跳过）；
- 树节点文本均来自原始源码字节，与 JavaParser 的 toString/value 语义一致。
"""

from __future__ import annotations

import tree_sitter_java
from tree_sitter import Language, Parser

JAVA_LANGUAGE = Language(tree_sitter_java.language())
_PARSER = Parser(JAVA_LANGUAGE)

# 注解节点的两种形态（marker 无参 / 带参）
_ANN_TYPES = ("marker_annotation", "annotation")


def parse_java(content: str | None):
    """解析 Java 源码；语法错误返回 None（对齐 JavaParser isSuccessful=false）。"""
    if content is None:
        return None
    try:
        tree = _PARSER.parse(content.encode("utf-8"))
    except Exception:  # noqa: BLE001 空内容/编码异常按解析失败处理
        return None
    if tree.root_node is None or tree.root_node.has_error:
        return None
    return tree.root_node


def text(node) -> str:
    return node.text.decode("utf-8")


def field(node, name):
    return node.child_by_field_name(name)


def walk(node, *types):
    """前序 DFS 找全部指定类型节点（JavaParser findAll 等价，含起始节点自身）。"""
    out: list = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in types:
            out.append(n)
        # 前序顺序：named_children 逆序入栈
        stack.extend(reversed(n.named_children))
    return out


def modifiers_node(node):
    for c in node.children:
        if c.type == "modifiers":
            return c
    return None


def annotations_of(node) -> list:
    """节点（类/接口/方法/字段）上的注解列表（源码顺序）。"""
    m = modifiers_node(node)
    if m is None:
        return []
    return [c for c in m.named_children if c.type in _ANN_TYPES]


def ann_name(ann) -> str:
    n = field(ann, "name")
    return text(n) if n is not None else ""


def ann_pairs(ann) -> list[tuple[str | None, object]]:
    """注解成员列表：[(key, value_node)]；单成员形态 key 为 None。"""
    for c in ann.named_children:
        if c.type == "annotation_argument_list":
            out: list[tuple[str | None, object]] = []
            for el in c.named_children:
                if el.type == "element_value_pair":
                    k = field(el, "key")
                    out.append((text(k) if k is not None else None, field(el, "value")))
                else:
                    out.append((None, el))
            return out
    return []


def string_value(node) -> str | None:
    """字符串字面量的引号内原文（转义不展开，对齐 JavaParser StringLiteralExpr.getValue）。"""
    if node is None or node.type != "string_literal":
        return None
    for c in node.named_children:
        if c.type == "string_fragment":
            return text(c)
    return None


def first_string(e):
    """取字符串字面量；数组取首个字符串元素（对齐 JavaParser firstString）。"""
    if e is None:
        return None
    if e.type == "string_literal":
        return string_value(e)
    if e.type == "array_initializer":
        for c in e.named_children:
            if c.type == "string_literal":
                v = string_value(c)
                if v is not None:
                    return v
    return None


def first_string_in(node) -> str:
    """注解子树内第一个字符串字面量（Java findFirst(StringLiteralExpr) 兜底等价）。"""
    for c in walk(node, "string_literal"):
        v = string_value(c)
        if v is not None:
            return v
    return ""


def integer_literal_value(init) -> int | None:
    """整数字面量解析（Java Long.parseLong(init.toString()) 等价）。

    仅纯十进制数字成功：L 后缀/下划线/十六进制/超 long 范围均返回 None，
    调用方按「初始值不可用」静默跳过（对齐 Java NumberFormatException 分支）。
    """
    if init is None:
        return None
    if not init.type.endswith("_integer_literal"):
        return None
    t = text(init)
    if not t.isdigit():
        return None
    v = int(t)
    if v > 2**63 - 1:
        return None  # Java Long.parseLong 溢出抛异常 → 跳过
    return v


def body_children(node, *types) -> list:
    """类型声明的 body 直接子节点（getFields/getMethods 等价：仅直接成员，不含嵌套）。"""
    body = field(node, "body")
    if body is None:
        return []
    return [c for c in body.children if c.type in types]


def is_static_final(fd) -> bool:
    """字段是否同时带 static 与 final 修饰符。"""
    m = modifiers_node(fd)
    if m is None:
        return False
    tokens = [c.type for c in m.children]
    return "static" in tokens and "final" in tokens


def class_or_interface_nodes(root) -> list:
    """全部类型声明（JavaParser ClassOrInterfaceDeclaration 同时覆盖类与接口）。"""
    return walk(root, "class_declaration", "interface_declaration")
