"""T1.1 Language Adapter 接口契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ptde.analyzer import AnalyzerOutput, Finding, FindingStatus, LanguageAdapter
from ptde.models.access import AccessPattern
from ptde.models.base import Evidence, EvidenceStatus
from ptde.models.entity import EntityModel, RelationModel


class _DummyJavaAdapter(LanguageAdapter):
    """测试用最小实现：仅验证接口契约，不依赖 lang_java。"""

    name = "dummy-java"

    def supports(self, project_root: Path) -> bool:
        return (project_root / "pom.xml").exists()

    def analyze(self, paths: list[Path]) -> AnalyzerOutput:
        ev = Evidence(type="source_mapping", file="Order.java", line=1, status=EvidenceStatus.CONFIRMED)
        rel = RelationModel(
            child_table="orders",
            child_column="user_id",
            parent_table="users",
            parent_column="id",
            status=EvidenceStatus.CONFIRMED,
            confidence=1.0,
            evidence=[ev],
        )
        finding = Finding(
            id="f1",
            type="relation",
            value={"child_table": "orders", "parent_table": "users"},
            source_file="Order.java",
            evidence=[ev],
            confidence=1.0,
            status=FindingStatus.CONFIRMED,
            analyzer_version="0.1.0",
        )
        out = AnalyzerOutput(
            id="out-1",
            analyzer_version="0.1.0",
            entities=[EntityModel(id="order", table="orders", evidence=[ev])],
            relations=[rel],
            access_patterns=[
                AccessPattern(id="ap-1", tables=["orders"], source_refs=["OrderMapper.xml"])
            ],
            findings=[finding],
        )
        out.seal()
        return out


def test_analyzer_output_is_structured_and_fingerprinted() -> None:
    out = _DummyJavaAdapter().analyze([Path(".")])
    assert out.fingerprint
    assert out.version
    # 输出为结构化模型而非文本
    assert out.relations[0].child_table == "orders"
    assert out.relations[0].status == EvidenceStatus.CONFIRMED
    assert out.access_patterns[0].tables == ["orders"]
    # JSON 往返不丢字段
    data = out.to_json()
    out2 = AnalyzerOutput.from_json(data)
    assert out2.fingerprint == out.fingerprint
    assert out2.relations[0].child_column == "user_id"


def test_finding_three_state_and_structured_evidence() -> None:
    f = Finding(id="f", type="enum", value={"values": ["A", "B"]}, source_file="X.java")
    assert f.status == FindingStatus.UNKNOWN
    # 证据必须为结构化引用：source_file / database_object 至少其一可追溯
    f2 = Finding(
        id="f2",
        type="relation",
        value={},
        database_object="mysql:orders.fk",
        evidence=[Evidence(type="ddl", object_name="fk_orders_user")],
    )
    assert f2.evidence[0].object_name == "fk_orders_user"


def test_finding_maps_to_evidence_status() -> None:
    assert Finding(id="x", type="t", status=FindingStatus.INFERRED).to_evidence_status() == (
        EvidenceStatus.INFERRED
    )


def test_language_adapter_requires_subclass_implementations() -> None:
    with pytest.raises(TypeError):
        LanguageAdapter()  # type: ignore[abstract]


def test_adapter_supports_gate_on_project_structure() -> None:
    adapter = _DummyJavaAdapter()
    assert adapter.supports(Path("/workspace/perf-demo"))
    assert not adapter.supports(Path("/tmp/definitely-not-a-maven-project"))
