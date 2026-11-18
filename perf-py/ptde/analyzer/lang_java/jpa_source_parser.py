"""JPA 源码解析器：移植自 orm/jpa/JpaSourceParser.java。

解析 @Entity/@Table/@ManyToOne/@OneToMany/@JoinColumn/@JoinTable 构建关联语义。
覆盖范围：
- @ManyToOne/@OneToOne + @JoinColumn：子表外键列 → 父表主键（orm_join，置信 0.9）
- @OneToMany/@ManyToMany + @JoinTable：中间表两侧外键（orm_join，置信 0.9）
- @OneToMany(mappedBy=...)：反向声明，由对端 ManyToOne 覆盖，不重复产出
- Java 枚举与状态常量：复用 JavaSourceHints（与 MyBatis 一致）

容错策略同 MyBatis：单文件解析失败静默跳过，绝不中断灌数流程。
"""

from __future__ import annotations

import time

from ptde.analyzer.lang_java import java_ast as ja
from ptde.analyzer.lang_java._legacy_model import RelationHint, SourceFile
from ptde.analyzer.lang_java.java_source_hints import (
    collect_enums_and_constants,
    snake,
    to_table_name,
)
from ptde.analyzer.lang_java.orm_source_parser import ScanResult

FILE_TIMEOUT_MS = 5_000
# jakarta / javax 持久化包特征
JPA_PACKAGES = ("jakarta.persistence.", "javax.persistence.")


class JpaSourceParser:

    def supports(self, files: list[SourceFile] | None) -> bool:
        if files is None:
            return False
        for f in files:
            c = f.content
            if c is None:
                continue
            if any(pkg in c for pkg in JPA_PACKAGES):
                return True
        return False

    def order(self) -> int:
        return 20

    def scan(self, files: list[SourceFile], known_tables) -> ScanResult:
        result = ScanResult()
        if not files:
            return result
        deadline = time.monotonic() * 1000 + FILE_TIMEOUT_MS * max(1, len(files) // 20 + 1)
        for f in files:
            if time.monotonic() * 1000 > deadline:
                break  # 整体超时，剩余文件跳过
            if f.path is None or not f.path.lower().endswith(".java"):
                continue
            try:
                self._scan_java(f, known_tables, result)
            except Exception:  # noqa: BLE001 单文件失败静默跳过
                pass
        return result

    def _scan_java(self, f: SourceFile, known_tables, result: ScanResult) -> None:
        root = ja.parse_java(f.content)
        if root is None:
            return
        source_ref = f.path

        # 枚举与常量（与 MyBatis 共用片段）
        collect_enums_and_constants(root, source_ref, result)

        # 第一遍：实体类名 → 表名（@Table(name) 优先，否则类名 snake_case）
        entity_tables: dict[str, str] = {}
        entities = []
        for cd in ja.class_or_interface_nodes(root):
            if not _has_annotation(cd, "Entity"):
                continue
            entities.append(cd)
            table = _annotation_string(cd, "Table", "name")
            if table is None or not table.strip():
                table = to_table_name(ja.text(ja.field(cd, "name")))
            if table is not None:
                entity_tables[ja.text(ja.field(cd, "name"))] = table.lower()

        # 第二遍：关联声明
        for cd in entities:
            child_table = entity_tables.get(ja.text(ja.field(cd, "name")))
            if child_table is None:
                continue
            for fd in ja.body_children(cd, "field_declaration"):
                vd_list = [c for c in fd.named_children if c.type == "variable_declarator"]
                if not vd_list:
                    continue
                field_name = ja.text(ja.field(vd_list[0], "name"))
                target_entity = _target_entity_name(fd)
                if target_entity is None:
                    continue
                parent_table = entity_tables.get(target_entity, to_table_name(target_entity))

                if _has_annotation(fd, "ManyToOne") or _has_annotation(fd, "OneToOne"):
                    join = _parse_join_column(_annotation(fd, "JoinColumn"))
                    column = join[0] if join[0] is not None else snake(field_name) + "_id"
                    parent_column = join[1] if join[1] is not None else "id"
                    _add_hint(result, known_tables, child_table, column,
                              parent_table, parent_column, source_ref)
                elif _has_annotation(fd, "OneToMany") or _has_annotation(fd, "ManyToMany"):
                    join_table = _annotation(fd, "JoinTable")
                    if join_table is None:
                        continue  # mappedBy 反向声明：由对端 ManyToOne 覆盖
                    jt_name = _string_member(join_table, "name")
                    if jt_name is None or not jt_name.strip():
                        continue
                    join_table_ = jt_name.lower()
                    owner_side = _parse_join_column(_member_value(join_table, "joinColumns"))
                    target_side = _parse_join_column(_member_value(join_table, "inverseJoinColumns"))
                    # 中间表 → 拥有方（本实体表）
                    _add_hint(result, known_tables, join_table_,
                              owner_side[0] if owner_side[0] is not None else snake(child_table) + "_id",
                              child_table,
                              owner_side[1] if owner_side[1] is not None else "id", source_ref)
                    # 中间表 → 目标方
                    _add_hint(result, known_tables, join_table_,
                              target_side[0] if target_side[0] is not None else snake(parent_table or "") + "_id",
                              parent_table,
                              target_side[1] if target_side[1] is not None else "id", source_ref)


def _add_hint(result: ScanResult, known_tables, child_table, child_column,
              parent_table, parent_column, source_ref) -> None:
    if child_table is None or parent_table is None:
        return
    if child_table not in known_tables or parent_table not in known_tables:
        return
    h = RelationHint(child_table, child_column, parent_table, parent_column,
                     RelationHint.SRC_ORM_JOIN, 0.9)
    h.source_refs.append(source_ref)
    result.relation_hints.append(h)


def _target_entity_name(fd):
    """字段的目标实体类名（ManyToOne 取类型；List<X>/Set<X> 取泛型首个实参）。"""
    type_node = ja.field(fd, "type")
    if type_node is None:
        return None
    if type_node.type == "type_identifier":
        return ja.text(type_node)
    if type_node.type == "generic_type":
        args = [c for c in type_node.named_children if c.type == "type_arguments"]
        if args:
            inner = args[0].named_children
            if inner:
                return ja.text(inner[0])
        return ja.text(type_node)
    return None


def _has_annotation(node, name: str) -> bool:
    return _annotation(node, name) is not None


def _annotation(node, name: str):
    for a in ja.annotations_of(node):
        if ja.ann_name(a) == name:
            return a
    return None


def _annotation_string(cd, ann: str, key: str) -> str | None:
    a = _annotation(cd, ann)
    return _string_member(a, key) if a is not None else None


def _member_value(ann, key: str):
    """注解成员值节点（name / joinColumns / inverseJoinColumns）。"""
    for k, v in ja.ann_pairs(ann):
        if k == key:
            return v
    return None


def _string_member(ann, key: str) -> str | None:
    for k, v in ja.ann_pairs(ann):
        if (k is None and key == "name") or k == key:
            return ja.string_value(v)
    return None


def _parse_join_column(e):
    """@JoinColumn(name="user_id", referencedColumnName="id") → [name, referencedColumnName]。"""
    out: list[str | None] = [None, None]
    if e is None:
        return out
    if e.type == "annotation":
        for k, v in ja.ann_pairs(e):
            if k is None:
                out[0] = ja.string_value(v)
            elif k == "name":
                out[0] = ja.string_value(v)
            elif k == "referencedColumnName":
                out[1] = ja.string_value(v)
        return out
    if e.type == "array_initializer":
        # joinColumns = { @JoinColumn(name = "...") }
        for item in e.named_children:
            if item.type == "annotation":
                return _parse_join_column(item)
    return out
