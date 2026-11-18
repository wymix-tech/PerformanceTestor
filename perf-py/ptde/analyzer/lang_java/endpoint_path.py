"""HTTP 接口路径归一化单点工具：移植自 util/EndpointPath.java。

宿主存在两条接口解析链路（正则实现与 AST 实现），两者若各自实现归一化，
一旦出现分歧（尾斜杠、多值 path、缺前导斜杠），按 (httpMethod, path) 的精确
匹配会静默降级且难以定位。故收敛为本模块，两条链路共用，保证产出完全一致。

路径变量（{id}、{userId}）原样保留，不做替换——替换后会与 Spring 的 /{id}
语义错位。
"""

from __future__ import annotations


def join(class_prefix: str | None, method_path: str | None) -> str:
    """拼接类级前缀与方法级路径，产出完整路径；两者皆空时返回 "/"。"""
    a = normalize(class_prefix)
    b = normalize(method_path)
    if not a and not b:
        return "/"
    joined = a + b
    return "/" if not joined else strip_trailing_slash(joined)


def normalize(p: str | None) -> str:
    """单段路径归一化：去空白、补前导斜杠、去尾部斜杠（保留根 "/"）。"""
    if p is None or not p.strip():
        return ""
    s = p.strip()
    if not s.startswith("/"):
        s = "/" + s
    return strip_trailing_slash(s)


def strip_trailing_slash(s: str | None) -> str:
    """去尾部斜杠，保留根 "/"。"""
    if not s:
        return ""
    r = s
    while r.endswith("/") and len(r) > 1:
        r = r[:-1]
    return r


def simple_name_of(file_path: str | None) -> str:
    """从文件路径取 simpleName（去目录与 .java 后缀）。"""
    if file_path is None:
        return ""
    name = file_path
    slash = max(name.rfind("/"), name.rfind("\\"))
    if slash >= 0:
        name = name[slash + 1:]
    if name.endswith(".java"):
        name = name[: -len(".java")]
    return name
