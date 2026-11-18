"""关联解析四级合并 + 三态化输出（T1.3 收编 `datagen/relation_resolver.py`）。

四级来源优先级（高 → 低）：explicit 40 > db_fk 30 > orm_join 20 > naming 10。
与 legacy 的行为差异（A3 分级显式，唯一差异点）：

- db_fk / explicit → `CONFIRMED`（数据库事实 / 用户显式规则）；
- orm_join（MyBatis/JPA 注解解析产物）→ `INFERRED`（注解 SQL 推断，不得伪装为
  数据库事实；沿用 legacy 自动合入边集的语义，但状态显式标记）；
- naming → `INFERRED` 且不自动生效，进入采纳流（pending）；
- **禁止静默跳过**：被更高优先级来源覆盖的 naming 边也显式登记进 findings
  （superseded），不再像 legacy 那样直接丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ptde.analyzer.lang_java._legacy_model import RelationHint
from ptde.analyzer.language_adapter import Finding, FindingStatus
from ptde.analyzer.naming_resolver import NamingRelationResolver
from ptde.models.base import Evidence, EvidenceStatus
from ptde.models.entity import Cardinality, RelationKind, RelationModel
from ptde.models.snapshot import TableSchema

__all__ = ["RelationResolution", "RelationResolver"]

ANALYZER_VERSION = "0.1.0"


@dataclass
class RelationResolution:
    """解析产物：三态关系边集 + 待采纳建议 + 显式发现登记（无静默跳过）。"""

    relations: list[RelationModel] = field(default_factory=list)
    pending: list[RelationModel] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _edge_key(h: RelationHint) -> str:
    return f"{h.child_table}.{h.child_column}"


def _better(a: RelationHint, b: RelationHint) -> RelationHint:
    return a if a.priority() >= b.priority() else b


def _merge(best: dict[str, RelationHint], h: RelationHint,
           superseded: list[RelationHint] | None = None) -> None:
    """对齐 legacy Map.merge：同键取高优先级；被覆盖边显式登记（禁止静默跳过）。"""
    key = _edge_key(h)
    existing = best.get(key)
    if existing is None:
        best[key] = h
        return
    winner = _better(existing, h)
    loser = h if winner is existing else existing
    if superseded is not None and loser is not winner:
        superseded.append(loser)
    best[key] = winner


class RelationResolver:
    """四级合并解析器（无状态；输入为 DatabaseSchemaSnapshot.tables）。"""

    def __init__(self) -> None:
        self._naming = NamingRelationResolver()

    def resolve(
        self,
        tables: list[TableSchema],
        orm_hints: list[RelationHint] | None = None,
        explicit_hints: list[RelationHint] | None = None,
        conn=None,
        dialect=None,
    ) -> RelationResolution:
        """合并四级来源，产出三态关系边集与发现登记。

        explicit_hints：用户显式规则边（RelationHint，SRC_EXPLICIT 语义）；
        orm_hints：lang_java 解析器产出的注解关联（SRC_ORM_JOIN 语义）。
        """
        best: dict[str, RelationHint] = {}
        superseded: list[RelationHint] = []

        # 来源 2：db_fk 声明外键（CONFIRMED，数据库事实）
        for t in tables:
            for fk in t.foreign_keys:
                for i, child_col in enumerate(fk.child_columns):
                    pk_col = fk.parent_columns[i] if i < len(fk.parent_columns) else ""
                    h = RelationHint(
                        t.name, child_col, fk.parent_table, pk_col,
                        RelationHint.SRC_DB_FK, 1.0,
                    )
                    h.source_refs.append(f"metadata:{fk.name or 'fk'}")
                    _merge(best, h, superseded)

        # 来源 3：orm_join 源码语义（INFERRED，参数化传入，无共享状态）
        for h in orm_hints or []:
            _merge(best, h, superseded)

        # 来源 1：explicit 显式规则（最高优先级）
        for h in explicit_hints or []:
            _merge(best, h, superseded)

        # 来源 4：naming 命名推断（INFERRED，不自动生效 → pending）
        pending: list[RelationHint] = []
        for t in tables:
            parents = {o.name: o for o in tables if o.name != t.name}
            for rel in self._naming.resolve(t, parents, conn, dialect):
                h = RelationHint(
                    rel.child_table, rel.child_column, rel.parent_table, rel.parent_column,
                    RelationHint.SRC_NAMING, rel.confidence,
                )
                h.source_refs = [e.detail or "naming-convention" for e in rel.evidence]
                if _edge_key(h) in best:
                    superseded.append(h)
                    continue
                pending.append(h)

        # 覆盖边登记：naming 被高优先级覆盖 → 显式记录（禁止静默跳过）
        # （naming 优先级最低，merge 不会改变 best；此处仅登记不生效）

        out = RelationResolution()
        for h in best.values():
            rel = self._to_relation(h)
            out.relations.append(rel)
            out.findings.append(self._to_finding(rel, h))
        for h in pending:
            rel = self._to_relation(h)
            out.pending.append(rel)
            out.findings.append(self._to_finding(rel, h))
        for h in superseded:
            rel = self._to_relation(h)
            out.findings.append(self._to_finding(rel, h, superseded=True))
        return out

    # ---------- 三态映射 ----------

    @staticmethod
    def _to_relation(h: RelationHint) -> RelationModel:
        if h.source == RelationHint.SRC_DB_FK:
            kind = RelationKind.DB_FK
            status = EvidenceStatus.CONFIRMED
        elif h.source == RelationHint.SRC_EXPLICIT:
            kind = RelationKind.LOGICAL
            status = EvidenceStatus.CONFIRMED
        elif h.source == RelationHint.SRC_ORM_JOIN:
            kind = RelationKind.ORM
            status = EvidenceStatus.INFERRED  # 注解 SQL 推断：一律 INFERRED
        else:
            kind = RelationKind.LOGICAL
            status = EvidenceStatus.INFERRED  # naming 级一律 INFERRED
        ev = Evidence(
            type=_evidence_type(h.source),
            detail=";".join(h.source_refs) or h.source,
            status=status,
        )
        return RelationModel(
            child_table=h.child_table or "",
            child_column=h.child_column or "",
            parent_table=h.parent_table or "",
            parent_column=h.parent_column or "",
            cardinality=Cardinality.MANY_TO_ONE,
            relation_kind=kind,
            status=status,
            confidence=h.confidence,
            evidence=[ev],
        )

    @staticmethod
    def _to_finding(rel: RelationModel, h: RelationHint, superseded: bool = False) -> Finding:
        value = {
            "child_table": rel.child_table,
            "child_column": rel.child_column,
            "parent_table": rel.parent_table,
            "parent_column": rel.parent_column,
            "source": h.source,
            "superseded": superseded,
        }
        f = Finding(
            id=f"relation:{rel.child_table}.{rel.child_column}->{rel.parent_table}",
            type="relation",
            value=value,
            database_object=f"{rel.child_table}.{rel.child_column}",
            confidence=rel.confidence,
            status=FindingStatus(rel.status.value),
            analyzer_version=ANALYZER_VERSION,
        )
        f.evidence = list(rel.evidence)
        return f


def _evidence_type(source: str | None) -> str:
    return {
        RelationHint.SRC_DB_FK: "database_foreign_key",
        RelationHint.SRC_EXPLICIT: "explicit_rule",
        RelationHint.SRC_ORM_JOIN: "source_mapping",
        RelationHint.SRC_NAMING: "naming_inference",
        RelationHint.SRC_NAMING_EMPTY: "naming_inference",
    }.get(source or "", "naming_inference")
