"""JavaAdapter 端到端验收（B1 门禁：真实项目 perf-demo）。

验收点（实施方案 §4.5 门禁 1/2）：
- 实体、约束、访问模式、证据完整可导出；
- UNKNOWN/INFERRED 项显式标记、数量可统计；
- AI/推断不伪装为数据库事实（schema 字段可溯源到方言查询或 DDL 结构）。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from ptde.analyzer import JavaAdapter
from ptde.analyzer.lang_java._legacy_model import RelationHint
from ptde.models.snapshot import ColumnSchema, ForeignKeySchema, TableSchema

PERF_DEMO = Path("/workspace/perf-demo")

pytestmark = pytest.mark.skipif(
    not PERF_DEMO.exists(), reason="perf-demo 项目不存在"
)


def _demo_schema() -> list[TableSchema]:
    users = TableSchema(
        name="perf_user",
        columns=[
            ColumnSchema(name="id", type_name="bigint", primary_key=True),
            ColumnSchema(name="name", type_name="varchar(64)"),
            ColumnSchema(name="age", type_name="int"),
        ],
        primary_keys=["id"],
    )
    orders = TableSchema(
        name="perf_order",
        columns=[
            ColumnSchema(name="id", type_name="bigint", primary_key=True),
            ColumnSchema(name="user_id", type_name="bigint"),
            ColumnSchema(name="amount", type_name="decimal(18,2)"),
            ColumnSchema(name="status", type_name="varchar(16)"),
        ],
        primary_keys=["id"],
        foreign_keys=[ForeignKeySchema(
            name="fk_order_user",
            child_columns=["user_id"],
            parent_table="perf_user",
            parent_columns=["id"],
        )],
    )
    return [users, orders]


def _analyze():
    adapter = JavaAdapter(schema_tables=_demo_schema(), call_graph_deadline_ms=10000)
    return adapter.analyze([PERF_DEMO])


def test_supports_real_project() -> None:
    assert JavaAdapter().supports(PERF_DEMO)


def test_analyzer_output_sealed_and_structured() -> None:
    out = _analyze()
    assert out.fingerprint and out.version
    # JSON 导出往返（验收：证据完整可导出）
    data = out.to_json()
    assert len(data) > 0
    from ptde.analyzer.language_adapter import AnalyzerOutput
    out2 = AnalyzerOutput.from_json(data)
    assert out2.fingerprint == out.fingerprint


def test_entities_with_fields_and_relations() -> None:
    out = _analyze()
    tables = {e.table: e for e in out.entities}
    assert {"perf_user", "perf_order"} <= set(tables)
    order = tables["perf_order"]
    fields = {f.name for f in order.fields}
    assert {"id", "user_id", "amount", "status"} <= fields
    # 实体关系引用来自 db_fk 关系
    assert any(r.target == "perf_user" for r in order.relations)


def test_relations_three_state_no_ai_masquerade() -> None:
    """AI/推断结果不伪装为数据库事实（B1 门禁 2）。"""
    out = _analyze()
    rel = next(
        r for r in out.relations
        if r.child_table == "perf_order" and r.child_column == "user_id"
    )
    assert rel.status.value == "CONFIRMED"
    assert rel.relation_kind.value == "db_fk"
    # 证据可溯源：数据库外键对象名
    assert any(e.type == "database_foreign_key" for e in rel.evidence)


def test_orm_annotation_join_is_inferred_and_superseded_recorded() -> None:
    """注解 SQL JOIN 正则结果一律 INFERRED（A3）；被 db_fk 覆盖时显式登记。"""
    orm = RelationHint("perf_order", "user_id", "perf_user", "id",
                       RelationHint.SRC_ORM_JOIN, 0.9)
    from ptde.analyzer.relation_resolver import RelationResolver
    resolution = RelationResolver().resolve(_demo_schema(), orm_hints=[orm])
    sup = [f for f in resolution.findings
           if f.value.get("superseded") and f.value.get("source") == "orm_join"]
    assert sup
    assert sup[0].status.value == "INFERRED"


def test_access_patterns_from_mapper_sql() -> None:
    out = _analyze()
    assert out.access_patterns, "perf-demo 注解 SQL 应产出访问模式"
    order_aps = [ap for ap in out.access_patterns if "perf_order" in ap.tables]
    assert order_aps


def test_findings_counts_by_three_state() -> None:
    """UNKNOWN/INFERRED 项显式标记、数量可统计（B1 门禁 1）。"""
    out = _analyze()
    counter = Counter(f.status.value for f in out.findings)
    # 发现非空且三态可统计（键为 CONFIRMED/INFERRED/UNKNOWN）
    assert sum(counter.values()) == len(out.findings) > 0
    assert counter.get("CONFIRMED", 0) >= 1  # db_fk 关系事实
    # 所有 INFERRED 发现带结构化 value（非文本断言）
    for f in out.findings:
        if f.status.value == "INFERRED":
            assert isinstance(f.value, dict) and f.value


def test_enum_findings_from_check_constraint() -> None:
    schema = _demo_schema()
    from ptde.models.snapshot import CheckConstraintSchema
    for t in schema:
        if t.name == "perf_order":
            t.check_constraints.append(CheckConstraintSchema(
                name="chk_status", expression="((`status` in ('PENDING','PAID','SHIPPED')))"
            ))
    out = JavaAdapter(schema_tables=schema).analyze([PERF_DEMO])
    enum_f = [f for f in out.findings
              if f.type == "enum" and f.database_object == "perf_order.status"]
    assert enum_f
    assert enum_f[0].status.value == "CONFIRMED"
    assert enum_f[0].value["values"] == ["PENDING", "PAID", "SHIPPED"]


def test_magic_hints_registered_inferred() -> None:
    """魔法值候选 → INFERRED 显式登记（perf-demo Mapper SQL 全为 #{} 占位无字面量，
    构造 MagicHint 直接验证登记路径）。"""
    from ptde.analyzer.lang_java._legacy_model import MagicHint
    h = MagicHint("perf_order", "status", MagicHint.KIND_LITERAL)
    h.values.append("SHIPPED")
    h.source_refs.append("OrderMapper.xml")
    findings = JavaAdapter._magic_findings([h])
    assert len(findings) == 1
    assert findings[0].status.value == "INFERRED"
    assert findings[0].database_object == "perf_order.status"
    assert findings[0].value["values"] == ["SHIPPED"]


def test_migration_and_test_files_unknown_registered(tmp_path: Path) -> None:
    (tmp_path / "db/migration").mkdir(parents=True)
    (tmp_path / "db/migration/V1__a.sql").write_text("SELECT 1;")
    out = JavaAdapter().analyze([tmp_path])
    mig = [f for f in out.findings if f.type == "migration_file"]
    assert mig and all(f.status.value == "UNKNOWN" for f in mig)
