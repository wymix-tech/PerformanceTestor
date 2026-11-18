"""Java 源码语义扫描公共片段：移植自 orm/support/JavaSourceHints.java。

枚举常量反查、状态常量魔法值、Java 类名 → 表名转换。
MyBatis 与 JPA 解析器共用（ORM 语法差异仅体现在关联声明上，枚举/常量发现一致），
避免两份实现漂移。
"""

from __future__ import annotations

import re

from ptde.analyzer.lang_java import java_ast as ja
from ptde.analyzer.lang_java._legacy_model import MagicHint
from ptde.analyzer.lang_java.orm_source_parser import ScanResult

# static final 状态常量命名特征（Java name.matches 大小写不敏感）
_CONST_NAME = re.compile(r"(?i)(STATUS|STATE|TYPE|KIND|FLAG|LEVEL)[A-Z0-9_]*")


def collect_enums_and_constants(root, source_ref: str | None, result: ScanResult) -> None:
    """收集 Java 枚举常量与 static final 状态常量（魔法值候选）。"""
    if root is None:
        return
    # Java 枚举常量反查（枚举发现第四级来源）
    for ed in ja.walk(root, "enum_declaration"):
        name = ja.field(ed, "name")
        constants = [ja.field(c, "name") for c in ja.walk(ed, "enum_constant")]
        if name is not None and constants and all(constants):
            result.java_enums[ja.text(name)] = [ja.text(c) for c in constants]

    # static final 常量：STATUS_X/TYPE_X 等命名特征，值入魔法值候选
    for fd in ja.walk(root, "field_declaration"):
        if not ja.is_static_final(fd):
            continue
        for vd in fd.named_children:
            if vd.type != "variable_declarator":
                continue
            vd_name = ja.field(vd, "name")
            if vd_name is None or not _CONST_NAME.fullmatch(ja.text(vd_name)):
                continue
            init = ja.field(vd, "value")
            if init is None:
                continue
            h: MagicHint | None = None
            if init.type == "string_literal":
                v = ja.string_value(init)
                if v is not None:
                    h = MagicHint(None, snake(ja.text(vd_name)), MagicHint.KIND_CONSTANT)
                    h.values.append(v)
            else:
                v = ja.integer_literal_value(init)
                if v is not None:
                    h = MagicHint(None, snake(ja.text(vd_name)), MagicHint.KIND_CONSTANT)
                    h.values.append(v)
            if h is not None:
                h.source_refs.append(source_ref)
                result.magic_hints.append(h)


def snake(constant_name: str) -> str:
    """常量名 → 列名（STATUS_SHIPPED → status；USER_TYPE_A → type）。"""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", constant_name).lower()
    for t in s.split("_"):
        if t in ("status", "state", "type", "flag", "level", "kind"):
            return t
    return s


def to_table_name(java_type: str | None) -> str | None:
    """Java 类名/全限定名 → 表名（camelCase → snake_case，剥离 DO/Entity/PO 后缀）。

    对齐 Java String.replace：全量替换所有出现（含类名中段）。
    """
    if java_type is None or not java_type.strip():
        return None
    simple = java_type[java_type.rfind(".") + 1:] if "." in java_type else java_type
    simple = simple.replace("DO", "").replace("Entity", "").replace("PO", "")
    if not simple:
        return None
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", simple).lower()
