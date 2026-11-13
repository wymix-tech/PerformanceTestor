"""Controller 接口索引（tree-sitter AST 实现）：移植自 endpoint/EndpointIndexParser.java。

宿主已有正则实现，本类不替换它，而是提供方法级 AST 坐标，供「按接口分析调用链」
精确定位入口方法。两者产出必须逐字段一致。

一致性要点（实现时严格对齐正则实现）：
- className 取 simpleName（文件名去 .java），不是 FQCN；
- 路径经 EndpointPath 单点归一化，{id} 变量原样保留；
- 路径提取顺序：value/path 成员 → 注解内第一个字符串字面量（与正则兜底行为一致）；
- line 取映射注解所在行；
- 单文件解析失败静默跳过，不中断整体扫描。
"""

from __future__ import annotations

from datagen import java_ast as ja
from datagen.endpoint_path import join, normalize, simple_name_of
from datagen.model import EndpointRef, SourceFile

CONTROLLER = "@Controller"
REST_CONTROLLER = "@RestController"
REQUEST_MAPPING = "RequestMapping"
# 方法级映射注解名 → HTTP 方法（RequestMapping 需再判 method 属性）
MAPPINGS = ("GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping")


class EndpointIndexParser:

    def parse(self, java_files: list[SourceFile] | None) -> list[EndpointRef]:
        """解析全部 Controller 接口（未排序，顺序与入参文件顺序一致）。"""
        out: list[EndpointRef] = []
        if java_files is None:
            return out
        for f in java_files:
            if not _is_java(f) or not _is_controller(f.content):
                continue
            try:
                self._parse_file(f, out)
            except Exception:  # noqa: BLE001 单文件失败静默跳过
                pass
        return out

    def _parse_file(self, f: SourceFile, out: list[EndpointRef]) -> None:
        root = ja.parse_java(f.content)
        if root is None:
            return
        class_name = simple_name_of(f.path)
        for type_node in ja.class_or_interface_nodes(root):
            class_prefix = normalize(self._class_mapping_path(type_node))
            for m in ja.body_children(type_node, "method_declaration"):
                for anno in ja.annotations_of(m):
                    http_method = self._http_method_of(anno)
                    if http_method is None:
                        continue
                    path = join(class_prefix, self._extract_path(anno))
                    method_name = ja.field(m, "name")
                    out.append(EndpointRef(
                        http_method,
                        path,
                        class_name,
                        ja.text(method_name) if method_name is not None else "",
                        f.path,
                        anno.start_point[0] + 1,
                    ))

    # 类级 @RequestMapping 路径（无则空串）
    def _class_mapping_path(self, type_node) -> str:
        for anno in ja.annotations_of(type_node):
            if REQUEST_MAPPING == ja.ann_name(anno):
                return self._extract_path(anno)
        return ""

    def _http_method_of(self, anno) -> str | None:
        """映射注解对应的 HTTP 方法；非映射注解返回 None。"""
        name = ja.ann_name(anno)
        for m in MAPPINGS:
            if m == name:
                return m[: -len("Mapping")].upper()
        if REQUEST_MAPPING != name:
            return None
        # @RequestMapping(method = RequestMethod.POST)：显式指定优先，缺省 GET（与正则实现一致）
        t = ja.text(anno).upper()
        idx = t.find("REQUESTMETHOD.")
        if idx >= 0:
            tail = t[idx + len("REQUESTMETHOD."):]
            for m in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                if tail.startswith(m):
                    return m
        return "GET"

    # 路径提取：value/path 成员优先，回退到注解内第一个字符串字面量
    def _extract_path(self, anno) -> str:
        for key, value in ja.ann_pairs(anno):
            if key in ("value", "path"):
                v = ja.first_string(value)
                if v is not None:
                    return v
        return ja.first_string_in(anno)


def _is_java(f: SourceFile | None) -> bool:
    return f is not None and f.path is not None and f.path.lower().endswith(".java")


def _is_controller(content: str | None) -> bool:
    return content is not None and (CONTROLLER in content or REST_CONTROLLER in content)
