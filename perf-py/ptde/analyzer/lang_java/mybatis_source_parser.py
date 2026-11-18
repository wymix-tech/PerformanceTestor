"""MyBatis 源码解析器：移植自 orm/mybatis/MyBatisSourceParser.java。

容错策略（防 SQL 碎片化）：
- 关联识别以 XML <resultMap> 的 <association>/<collection> 为主（结构化声明，成功率 100%）；
- 注解 SQL 的 JOIN 仅简单正则辅助，匹配失败静默跳过；
- 扫描整体超时强制跳过，不中断灌数。

XML 解析用标准库 ElementTree（不加载外部 DTD，对齐 Java 端 load-external-dtd=false）。
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

from ptde.analyzer.lang_java import java_ast as ja
from ptde.analyzer.lang_java._legacy_model import MagicHint, RelationHint, SourceFile
from ptde.analyzer.lang_java.java_source_hints import (  # noqa: F401
    collect_enums_and_constants,
    snake,
    to_table_name,
)
from ptde.analyzer.lang_java.orm_source_parser import ScanResult
from ptde.analyzer.lang_java.sql_literal_extractor import extract_join_relations, extract_literals

# 单文件解析超时（ms，Java 用文件数分摊：每 20 文件一档）
FILE_TIMEOUT_MS = 5_000
# mapper DTD 特征
MYBATIS_DTD = "-//mybatis.org//DTD Mapper"
# @Select/@Insert/@Update 注解特征
ANNOTATION_SQL = re.compile(r"@(Select|Insert|Update|Delete)\s*\(")
_SQL_ANN_NAMES = ("Select", "Insert", "Update", "Delete")


class MyBatisSourceParser:

    def supports(self, files: list[SourceFile] | None) -> bool:
        if files is None:
            return False
        for f in files:
            c = f.content
            if c is None:
                continue
            if f.path is not None and f.path.lower().endswith(".xml") and MYBATIS_DTD in c:
                return True
            if "org.apache.ibatis" in c or ANNOTATION_SQL.search(c):
                return True
        return False

    def order(self) -> int:
        return 10

    def scan(self, files: list[SourceFile], known_tables) -> ScanResult:
        result = ScanResult()
        if not files:
            return result
        deadline = time.monotonic() * 1000 + FILE_TIMEOUT_MS * max(1, len(files) // 20 + 1)
        for f in files:
            if time.monotonic() * 1000 > deadline:
                break  # 整体超时，剩余文件跳过（Java 侧有 warn 日志）
            try:
                if f.path is not None and f.path.lower().endswith(".xml"):
                    self._scan_xml(f, known_tables, result)
                elif f.path is not None and f.path.lower().endswith(".java"):
                    self._scan_java(f, known_tables, result)
            except Exception:  # noqa: BLE001 单文件失败静默跳过（防 SQL 方言碎片化中断灌数）
                pass
        return result

    # Java 源码：@Select 注解 SQL 字面量/JOIN + static final 常量 + enum 常量反查
    def _scan_java(self, f: SourceFile, known_tables, result: ScanResult) -> None:
        root = ja.parse_java(f.content)
        if root is None:
            return
        source_ref = f.path

        # Java 枚举常量反查 + static final 状态常量（与 JPA 解析器共用片段）
        collect_enums_and_constants(root, source_ref, result)

        # @Select/@Insert/@Update/@Delete 注解 SQL（字符串常量；provider 等形态静默跳过）
        for ann in ja.walk(root, "annotation"):
            if ja.ann_name(ann) not in _SQL_ANN_NAMES:
                continue
            sql: str | None = None
            for key, value in ja.ann_pairs(ann):
                if key is None or key == "value":
                    v = ja.string_value(value)
                    if v is not None:
                        sql = v
            if not sql or not sql.strip():
                continue
            result.magic_hints.extend(_collect_for_known_tables(sql, known_tables, source_ref))
            result.relation_hints.extend(extract_join_relations(sql, known_tables))

    # XML mapper：resultMap association/collection 关联 + select/insert/update 字面量与 JOIN
    def _scan_xml(self, f: SourceFile, known_tables, result: ScanResult) -> None:
        content = f.content
        if content is None or "<mapper" not in content:
            return
        # Java 文本块会剔除首换行，Python 三引号字符串不会——XML 声明必须在实体开头
        doc = ET.fromstring(content.strip())
        source_ref = f.path

        # resultMap 关联声明（结构化，最可靠）
        for rm in doc.iter("resultMap"):
            child_table = to_table_name(rm.get("type"))
            for tag in rm:
                if tag.tag not in ("association", "collection"):
                    continue
                column = tag.get("column") or ""
                java_type = tag.get("javaType") or ""
                p_type = java_type if java_type.strip() else (tag.get("ofType") or "")
                parent_table = to_table_name(p_type)
                if not column.strip() or parent_table is None or child_table is None:
                    continue
                if child_table not in known_tables or parent_table not in known_tables:
                    continue
                h = RelationHint(child_table, column, parent_table, "id",
                                 RelationHint.SRC_ORM_JOIN, 0.9)
                h.source_refs.append(source_ref)
                result.relation_hints.append(h)

        # SQL 文本节点：字面量 + JOIN（正则辅助）
        for tag in ("select", "insert", "update", "delete"):
            for el in doc.iter(tag):
                sql = "".join(el.itertext())
                result.magic_hints.extend(_collect_for_known_tables(sql, known_tables, source_ref))
                result.relation_hints.extend(extract_join_relations(sql, known_tables))


def _collect_for_known_tables(sql: str, known_tables, source_ref: str | None) -> list[MagicHint]:
    hints: list[MagicHint] = []
    for t in known_tables:
        for h in extract_literals(sql, t, source_ref):
            if h.values:
                hints.append(h)
    return hints
