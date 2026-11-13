"""关联关系解析：移植自 engine/RelationResolver.java。

四级来源合并（explicit > db_fk > orm_join > naming）。
naming 系不自动生效，经 pendingSuggestions 交宿主确认采纳。

无状态契约：orm_join 语义经 resolve(..., orm_hints=...) 参数传入，解析器实例
可安全并发复用（修复旧实现 registerSource 实例字段在多任务并发时的状态互踩）。
"""

from __future__ import annotations

from datagen.explicit_rule_hints import extract as extract_explicit_hints
from datagen.model import DataGenSpec, RelationHint
from datagen.naming_relation_resolver import NamingRelationResolver


def _edge_key(h: RelationHint) -> str:
    return f"{h.child_table}.{h.child_column}"


def _better(a: RelationHint, b: RelationHint) -> RelationHint:
    return a if a.priority() >= b.priority() else b


def _merge(best: dict[str, RelationHint], h: RelationHint) -> None:
    """对齐 Java Map.merge(key, h, (old, v) -> better(old, v))：同键取高优先级。"""
    key = _edge_key(h)
    existing = best.get(key)
    best[key] = _better(existing, h) if existing is not None else h


class RelationResolution:
    """解析产物：自动生效边集 + 待确认建议。"""

    def __init__(self) -> None:
        self.auto_edges: list[RelationHint] = []
        self.pending_suggestions: list[RelationHint] = []


class RelationResolver:

    def __init__(self) -> None:
        self._naming_resolver = NamingRelationResolver()
        # 旧路径兼容字段（deprecated registerSource 注入）。对齐 Java 现状：
        # 4 参 resolve 实际传空 orm 列表，该字段已无读取点（Java 侧注释残留），
        # 保留仅为 API 兼容，不参与解析。
        self._orm_hints: list[RelationHint] = []

    def register_source(self, hints: list[RelationHint] | None) -> None:
        """deprecated：状态注入破坏无状态契约（并发任务互踩）。保留仅为兼容既有调用方。"""
        self._orm_hints = list(hints) if hints else []

    def resolve(self, metas, spec: DataGenSpec, conn=None, dialect=None,
                orm_hints: list[RelationHint] | None = None) -> RelationResolution:
        """解析表组关联，产出自动生效边集与待确认建议（orm_join 语义参数化，无共享状态）。"""
        safe_orm_hints = orm_hints if orm_hints is not None else []
        best: dict[str, RelationHint] = {}

        # 来源 2：db_fk 声明外键（自动应用）
        for m in metas:
            for fk in m.foreign_keys:
                h = RelationHint(m.table_name, fk.fk_column, fk.pk_table, fk.pk_column,
                                 RelationHint.SRC_DB_FK, 1.0)
                h.source_refs.append("metadata")
                _merge(best, h)

        # 来源 3：orm_join 源码语义（自动应用，参数化传入）
        for h in safe_orm_hints:
            _merge(best, h)

        # 来源 1：explicit 显式规则（最高优先级）
        for h in extract_explicit_hints(spec, {m.table_name: m for m in metas}):
            _merge(best, h)

        # 来源 4：naming 命名推断（父表空表时 naming_empty_fallback；低置信交确认）
        pending: list[RelationHint] = []
        for m in metas:
            parents = {other.table_name: other for other in metas
                       if other.table_name != m.table_name}
            for h in self._naming_resolver.resolve(m, parents, conn, dialect):
                if _edge_key(h) in best:
                    continue
                if h.auto_applicable():
                    best[_edge_key(h)] = h
                else:
                    pending.append(h)

        r = RelationResolution()
        r.auto_edges = list(best.values())
        r.pending_suggestions = pending
        return r
