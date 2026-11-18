"""Java Language Adapter（T1.1 本期唯一实现：组装 lang_java 解析器 + analyzer 组件）。

分析链路（对齐 legacy default_engine 六步流中的源码语义扫描，但输出三态模型）：
1. 收集 .java/.xml → SourceFile
2. MyBatis + JPA 双扫描（魔法值候选 / ORM 关联语义 / Java 枚举）
3. RelationResolver 四级合并（db_fk 来自 DatabaseSchemaSnapshot；orm/naming INFERRED）
4. EndpointIndex + CallGraph → Mapper SQL → AccessPattern（未解析登记 UNKNOWN）
5. EnumFinder 四级枚举发现（各来源独立证据）
6. 迁移文件 / 测试代码识别（UNKNOWN 登记，不解析）

容错：单文件/单端点失败不中断整体（legacy 同语义）。
"""

from __future__ import annotations

import time
from pathlib import Path

from ptde.analyzer.access_patterns import AccessPatternExtractor
from ptde.analyzer.enum_finder import EnumFinder
from ptde.analyzer.lang_java._legacy_model import RelationHint, SourceFile
from ptde.analyzer.lang_java.call_graph_analyzer import CallGraphAnalyzer
from ptde.analyzer.lang_java.endpoint_index_parser import EndpointIndexParser
from ptde.analyzer.lang_java.jpa_source_parser import JpaSourceParser
from ptde.analyzer.lang_java.layout import detect_migration_and_test_files
from ptde.analyzer.lang_java.mybatis_source_parser import MyBatisSourceParser
from ptde.analyzer.lang_java.mybatis_sql_resolver import MyBatisSqlResolver
from ptde.analyzer.language_adapter import AnalyzerOutput, Finding, FindingStatus, LanguageAdapter
from ptde.analyzer.relation_resolver import ANALYZER_VERSION as REL_VERSION
from ptde.analyzer.relation_resolver import RelationResolver
from ptde.models.entity import EntityModel, FieldModel, RelationRef
from ptde.models.snapshot import TableSchema

__all__ = ["JavaAdapter"]

ANALYZER_VERSION = REL_VERSION
_SQL_KIND_WRITE = {"insert", "update", "delete"}


class JavaAdapter(LanguageAdapter):
    """Java 栈适配器（MyBatis/JPA）；schema_tables 提供数据库事实（可空）。"""

    name = "java"

    def __init__(
        self,
        *,
        known_tables: set[str] | None = None,
        schema_tables: list[TableSchema] | None = None,
        conn=None,
        dialect=None,
        call_graph_deadline_ms: float | None = None,
    ) -> None:
        self._known_tables = {t.lower() for t in known_tables} if known_tables else None
        self._schema_tables = schema_tables
        self._conn = conn
        self._dialect = dialect
        # 调用链分析时长预算（ms）；CallGraphAnalyzer 的 deadline 参数是绝对单调时钟值
        self._cg_duration_ms = call_graph_deadline_ms

    def supports(self, project_root: Path) -> bool:
        root = Path(project_root)
        if (root / "pom.xml").exists() or (root / "build.gradle").exists():
            return True
        return any(root.rglob("*.java"))

    def analyze(self, paths: list[Path]) -> AnalyzerOutput:
        files = self._collect_files(paths)
        java_files = [f for f in files if (f.path or "").endswith(".java")]
        xml_files = [f for f in files if (f.path or "").endswith(".xml")]
        known = self._known_tables or self._tables_from_schema()

        # 1/2. ORM 扫描
        mybatis = MyBatisSourceParser().scan(files, known)
        jpa = JpaSourceParser().scan(files, known)
        orm_hints = [*mybatis.relation_hints, *jpa.relation_hints]
        java_enums: dict[str, list[str]] = {}
        java_enums.update(mybatis.java_enums)
        java_enums.update(jpa.java_enums)

        # 3. 关联四级合并 + 三态化
        resolution = RelationResolver().resolve(
            self._schema_tables or [], orm_hints=orm_hints,
            conn=self._conn, dialect=self._dialect,
        )

        # 4. 端点 → 调用链 → SQL → 访问模式
        access_patterns = self._extract_access_patterns(java_files, xml_files, known)

        # 5. 枚举四级发现
        enum_findings = self._find_enums(java_enums)

        # 6. 迁移文件 / 测试代码识别（UNKNOWN 登记）
        layout_findings = detect_migration_and_test_files(paths)

        findings = [
            *resolution.findings,
            *self._magic_findings(mybatis.magic_hints + jpa.magic_hints),
            *access_patterns[1],
            *enum_findings,
            *layout_findings,
        ]
        out = AnalyzerOutput(
            id="",
            analyzer_version=ANALYZER_VERSION,
            entities=self._build_entities(resolution.relations),
            relations=resolution.relations,
            access_patterns=access_patterns[0],
            findings=findings,
        )
        out.seal()
        return out

    # ---------- 内部步骤 ----------

    @staticmethod
    def _collect_files(paths: list[Path]) -> list[SourceFile]:
        files: list[SourceFile] = []
        seen: set[str] = set()
        for raw in paths:
            p = Path(raw)
            candidates = sorted(p.rglob("*")) if p.is_dir() else [p]
            for f in candidates:
                if not f.is_file() or f.suffix.lower() not in (".java", ".xml"):
                    continue
                rel = f.name if str(f) == f.name else str(f)
                if rel in seen:
                    continue
                seen.add(rel)
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue  # 不可读文件静默跳过（legacy 同语义）
                files.append(SourceFile(path=rel, content=content))
        return files

    def _tables_from_schema(self) -> set[str]:
        if not self._schema_tables:
            return set()
        return {t.name.lower() for t in self._schema_tables}

    def _extract_access_patterns(self, java_files, xml_files, known):
        extractor = AccessPatternExtractor()
        patterns: list = []
        findings: list[Finding] = []
        # XML 与注解式 SQL 双索引（legacy datagen_plan_service 同用法）
        resolver = MyBatisSqlResolver(xml_files or [])
        if java_files:
            resolver.index_annotations(java_files)
        endpoints = EndpointIndexParser().parse(java_files)
        deadline = (
            time.monotonic() * 1000 + self._cg_duration_ms
            if self._cg_duration_ms is not None else None
        )
        cg = CallGraphAnalyzer(deadline_ms=deadline)
        for ep in endpoints:
            try:
                result = cg.analyze(java_files, ep)
            except Exception:  # noqa: BLE001 单端点失败不中断
                continue
            for inv in result.invocations:
                sql = resolver.resolve_invocation(inv) if resolver else None
                if not sql or not sql.strip():
                    continue
                ref = inv.call_site  # 调用点证据：文件:行号
                kind = "WRITE" if _is_write_sql(sql) else "READ"
                ap = extractor.extract(
                    sql, pattern_id=f"ap:{ep.key()}:{len(patterns)}",
                    sql_kind=kind, source_ref=str(ref), target_table=None,
                )
                patterns.append(ap)
                if ap.unresolved:
                    findings.append(Finding(
                        id=f"access_pattern:{ap.id}",
                        type="access_pattern",
                        value={"pattern_id": ap.id, "reason": ap.unresolved_reason},
                        source_file=str(ref),
                        status=FindingStatus.UNKNOWN,
                        analyzer_version=ANALYZER_VERSION,
                    ))
        return patterns, findings

    @staticmethod
    def _magic_findings(hints) -> list[Finding]:
        """魔法值候选 → INFERRED 登记（A3 推断类：warning + 采纳流，不静默丢弃）。"""
        findings: list[Finding] = []
        for h in hints:
            if not h.table or not h.column:
                continue
            findings.append(Finding(
                id=f"magic_hint:{h.table}.{h.column}:{h.kind}",
                type="magic_hint",
                value={
                    "table": h.table,
                    "column": h.column,
                    "kind": h.kind,
                    "values": list(h.values),
                },
                source_file=h.source_refs[0] if h.source_refs else None,
                database_object=f"{h.table}.{h.column}",
                confidence=0.6,
                status=FindingStatus.INFERRED,
                analyzer_version=ANALYZER_VERSION,
            ))
        return findings

    def _find_enums(self, java_enums: dict[str, list[str]]):
        finder = EnumFinder()
        findings: list[Finding] = []
        for t in self._schema_tables or []:
            check_map = self._check_by_column(t)
            for col in t.columns:
                check = check_map.get(col.name.lower())
                findings.extend(finder.find(
                    t.name, col, check_clause=check,
                    java_enums=java_enums or None,
                    conn=self._conn, dialect=self._dialect,
                ))
        return findings

    @staticmethod
    def _check_by_column(t: TableSchema) -> dict[str, str]:
        """CHECK 约束文本按列名粗归属（约束内含列名即归属，多列约束共享）。"""
        m: dict[str, str] = {}
        for c in t.check_constraints:
            for col in t.columns:
                if col.name and col.name.lower() in (c.expression or "").lower():
                    m.setdefault(col.name.lower(), c.expression or "")
        return m

    def _build_entities(self, relations) -> list[EntityModel]:
        if not self._schema_tables:
            return []
        entities: list[EntityModel] = []
        for t in self._schema_tables:
            fields = [
                FieldModel(
                    name=c.name or "",
                    type=c.type_name or "",
                    nullable=c.nullable,
                    primary_key=c.primary_key,
                    length=c.length,
                    precision=c.precision,
                    scale=c.scale,
                )
                for c in t.columns
            ]
            refs = [
                RelationRef(
                    target=r.parent_table,
                    join_fields=[r.child_column],
                    cardinality="many_to_one",
                )
                for r in relations
                if r.child_table == t.name
            ]
            e = EntityModel(id=t.name, table=t.name, fields=fields, relations=refs)
            e.evidence = list(t.evidence)
            entities.append(e)
        return entities


def _is_write_sql(sql: str) -> bool:
    head = sql.lstrip()[:12].lower()
    return any(head.startswith(w) for w in _SQL_KIND_WRITE)


__all__ += ["RelationHint"]  # noqa: PLE0604  保持 re-export（供外部构造 explicit hints）
