"""Mapper 方法 → SQL 文本解析器（双通道）：移植自 endpoint/MyBatisSqlResolver.java。

- XML：按 <mapper namespace> + 语句 id 匹配（namespace 末段或全等匹配 Mapper 简单类名）
- 注解：@Select/@Insert/@Update/@Delete 的字符串字面量（数组多段与 "A"+"B"
  拼接均支持——perf-demo 及多数项目的注解 SQL 都是拼接写法，不处理会导致整条 SQL 丢失）

动态标签（<if>/<where>/<foreach>）取文本节点拼接——碎片化不会中断，
缺失片段只会让结论不完整，由下游 SqlTableExtractor 的 warning 出口暴露，不做静默猜测。
"""

from __future__ import annotations

import threading
import xml.etree.ElementTree as ET

from datagen import java_ast as ja
from datagen.model import SourceFile

SQL_ANNOTATIONS = ("Select", "Insert", "Update", "Delete")
STATEMENT_TAGS = ("select", "insert", "update", "delete", "sql")


class MapperInvocation:
    """Mapper 调用点：一个接口方法对 Mapper 的一次调用（CallGraphAnalyzer 产物）。"""

    def __init__(self, mapper_type: str, method_name: str, call_site: str,
                 mapper_file: str | None = None) -> None:
        self.mapper_type = mapper_type
        self.method_name = method_name
        self.call_site = call_site
        self.mapper_file = mapper_file or ""

    def __str__(self) -> str:
        return f"{self.mapper_type}#{self.method_name} @ {self.call_site}"


class MyBatisSqlResolver:

    def __init__(self, xml_files: list[SourceFile] | None) -> None:
        self._xml_files = xml_files if xml_files is not None else []
        # 注解式：文件路径 → (方法名 → SQL)
        self._annotation_sql: dict[str, dict[str, str]] = {}
        # XML 式：namespace 末段 → (语句 id → SQL)
        self._xml_sql: dict[str, dict[str, str]] = {}
        self._indexed = False
        self._lock = threading.Lock()

    def resolve(self, mapper_type: str | None, method_name: str,
                mapper_file: str | None = None) -> str | None:
        """解析 Mapper 方法的 SQL；未定位到返回 None。"""
        self._ensure_indexed()
        if mapper_file is not None and mapper_file.strip():
            by_method = self._annotation_sql.get(mapper_file)
            if by_method is not None:
                sql = by_method.get(method_name)
                if sql and sql.strip():
                    return sql
        if mapper_type is not None:
            by_id = self._xml_sql.get(mapper_type.lower())
            if by_id is not None:
                sql = by_id.get(method_name)
                if sql and sql.strip():
                    return sql
        return None

    def resolve_invocation(self, inv: MapperInvocation | None) -> str | None:
        """按 Mapper 调用点解析（便捷重载）。"""
        if inv is None:
            return None
        return self.resolve(inv.mapper_type, inv.method_name, inv.mapper_file)

    def _ensure_indexed(self) -> None:
        with self._lock:
            if self._indexed:
                return
            self._indexed = True
            for f in self._xml_files:
                try:
                    self._index_xml(f)
                except Exception:  # noqa: BLE001 单文件失败静默跳过
                    pass

    def _index_xml(self, f: SourceFile) -> None:
        content = f.content
        if content is None or "<mapper" not in content:
            return
        # Java 文本块会剔除首换行，Python 三引号字符串不会——XML 声明必须在实体开头
        doc = ET.fromstring(content.strip())
        mappers = list(doc.iter("mapper"))
        if not mappers:
            return
        namespace = mappers[0].get("namespace") or ""
        if not namespace.strip():
            return
        key = namespace[namespace.rfind(".") + 1:] if "." in namespace else namespace
        by_id = self._xml_sql.setdefault(key.lower(), {})
        for tag in STATEMENT_TAGS:
            for stmt in doc.iter(tag):
                id_ = stmt.get("id")
                if not id_ or not id_.strip():
                    continue
                if id_ not in by_id:
                    by_id[id_] = "".join(stmt.itertext())

    def index_annotations(self, mapper_files: list[SourceFile] | None) -> None:
        """解析 Mapper 接口的注解 SQL（宿主在拿到 Mapper 源码文件时调用）。"""
        if mapper_files is None:
            return
        for f in mapper_files:
            try:
                self._index_annotation_file(f)
            except Exception:  # noqa: BLE001 单文件失败静默跳过
                pass

    def _index_annotation_file(self, f: SourceFile) -> None:
        if f.content is None:
            return
        root = ja.parse_java(f.content)
        if root is None:
            return
        by_method = self._annotation_sql.setdefault(f.path, {})
        for m in ja.walk(root, "method_declaration"):
            for anno in ja.annotations_of(m):
                if ja.ann_name(anno) not in SQL_ANNOTATIONS:
                    continue
                sql = self._annotation_text(anno)
                if sql and sql.strip():
                    name = ja.field(m, "name")
                    key = ja.text(name) if name is not None else ""
                    if key not in by_method:
                        by_method[key] = sql

    @staticmethod
    def _annotation_text(anno) -> str | None:
        """取注解 SQL：单成员字符串、value 成员，或数组多段以空格拼接。"""
        parts: list[str] = []
        for k, v in ja.ann_pairs(anno):
            if k is None or k == "value":
                _collect_strings(v, parts)
        return " ".join(parts) if parts else None


def _collect_strings(e, out: list[str]) -> None:
    """收集注解中的 SQL 字符串：字符串字面量、数组、以及 "A" + "B" 拼接。"""
    if e is None:
        return
    if e.type == "string_literal":
        v = ja.string_value(e)
        if v is not None:
            out.append(v)
    elif e.type == "array_initializer":
        for item in e.named_children:
            _collect_strings(item, out)
    elif e.type == "binary_expression":
        left = ja.field(e, "left")
        right = ja.field(e, "right")
        op = ja.field(e, "operator")
        if op is not None and ja.text(op) == "+":
            _collect_strings(left, out)
            _collect_strings(right, out)
