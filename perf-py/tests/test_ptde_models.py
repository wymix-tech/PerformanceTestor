"""R0 验收：统一模型（ptde/models）——指纹、JSON/YAML 双读、依赖隔离（实施方案 §3.6 验收 2）。"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import ptde.models as m

ROOT = Path(__file__).resolve().parent.parent

# 各核心模型的代表性构造数据（字段取实施方案 §3.4 / 总方案 §5.2、§6.2 示例值）
SAMPLES: dict[str, dict] = {
    "DbRef": {"product": "mysql", "db_version": "8.0", "engine": "InnoDB"},
    "Evidence": {"type": "database_foreign_key", "object_name": "fk_orders_user"},
    "ProjectSnapshot": {"id": "snap-1", "source_revision": "abc123",
                        "analyzed_at": "2026-11-17T00:00:00Z"},
    "TableSchema": {"name": "orders", "primary_keys": ["id"],
                    "columns": [{"name": "id", "type_name": "bigint", "primary_key": True}],
                    "indexes": [{"name": "idx_user", "columns": [
                        {"name": "user_id", "ordinal": 1}, {"name": "status", "ordinal": 2}]}]},
    "DatabaseSchemaSnapshot": {"id": "schema-1", "database": {"product": "mysql"},
                               "tables": [{"name": "orders"}]},
    "EntityModel": {"id": "order", "table": "orders",
                    "fields": [{"name": "id", "type": "bigint", "primary_key": True}]},
    "RelationModel": {"child_table": "orders", "child_column": "user_id",
                      "parent_table": "users", "parent_column": "id",
                      "status": "INFERRED", "confidence": 0.4},
    "BusinessInvariant": {"id": "inv-1", "description": "支付单一致性", "status": "CONFIRMED",
                          "predicate": {"type": "relation_exists"}},
    "AccessPattern": {"id": "ap-1",
                      "filters": [{"column": "user_id", "op": "EQ", "values": [1]}]},
    "StatisticsProfile": {"id": "prof-1", "source": {"type": "file_upload", "sampling_rate": 0.1},
                          "tables": [{"table": "orders", "approximate_rows": 1000}]},
    "UnifiedDataProfile": {"id": "unified-1", "merge_report": {"conflicts": [
        {"subject": "orders.rows", "sources": ["a", "b"], "winner": "a", "reason": "x"}]}},
    "DatasetSpec": {"name": "ecommerce", "targets": {"rows": {"orders": 100000000}},
                    "constraints": {"max_database_connections": 8}},
    "GenerationPlan": {"id": "plan-1", "dataset_spec_fingerprint": "f" * 64,
                       "entities": {"orders": {"entity": "orders", "rows": 100,
                                               "source": "explicit"}}},
    "ExecutionManifest": {"id": "mf-1", "seed": 42, "generator_version": "0.1.0",
                          "model_fingerprints": {"schema": "a" * 64},
                          "actual_id_ranges": {"orders": {"start": 1, "end": 100}}},
    "DatasetDefinition": {"def_key": "ecom", "name": "ecom", "spec": {"name": "ecom"}},
    "DatasetInstance": {"id": "inst-1", "definition_id": "d1", "owner_token": "t" * 64},
    "ValidationReport": {"instance_id": "inst-1", "layers": [
        {"layer": "count", "status": "PASS", "checks": [{"name": "rows"}]}]},
    "TransitionRecord": {"from_state": "PLANNED", "to_state": "GENERATING",
                         "operator": "op", "reason": "start",
                         "occurred_at": datetime.now(UTC)},
}

ALL_MODELS = [
    m.DbRef, m.Evidence, m.ProjectSnapshot, m.TableSchema, m.DatabaseSchemaSnapshot,
    m.EntityModel, m.RelationModel, m.BusinessInvariant, m.AccessPattern,
    m.StatisticsProfile, m.UnifiedDataProfile, m.DatasetSpec, m.GenerationPlan,
    m.ExecutionManifest, m.DatasetDefinition, m.DatasetInstance, m.ValidationReport,
    m.TransitionRecord,
]


def _sample(name: str):
    return getattr(m, name).model_validate(SAMPLES[name])


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_versioned_models_have_version_fingerprint(name):
    """每个模型带 version 与 fingerprint 字段，且 fingerprint 为 64 位 sha256。"""
    instance = _sample(name)
    assert instance.version == "1"
    fp = instance.compute_fingerprint()
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)
    assert instance.seal().fingerprint == fp


def test_fingerprint_stable_and_sensitive():
    """同内容指纹稳定；任一字段变更指纹即变；易变字段（created_at）不影响。"""
    spec = _sample("DatasetSpec")
    fp1 = spec.compute_fingerprint()
    assert spec.model_copy(deep=True).compute_fingerprint() == fp1

    spec2 = spec.model_copy(deep=True)
    spec2.targets.rows["orders"] = 1
    assert spec2.compute_fingerprint() != fp1

    snap = _sample("ProjectSnapshot")
    snap.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    snap2 = _sample("ProjectSnapshot")
    assert snap2.compute_fingerprint() == snap.compute_fingerprint(), "created_at 不参与指纹"


@pytest.mark.parametrize("name", ["DatasetSpec", "ExecutionManifest", "DatasetInstance",
                                  "StatisticsProfile", "DatabaseSchemaSnapshot"])
def test_json_yaml_roundtrip(name):
    """JSON/YAML 双读往返：字段无丢失、指纹不变（A6）。"""
    instance = _sample(name).seal()

    from_json = type(instance).from_json(instance.to_json())
    from_yaml = type(instance).from_yaml(instance.to_yaml())

    assert from_json == instance
    assert from_yaml == instance
    assert from_json.compute_fingerprint() == instance.fingerprint
    assert from_yaml.compute_fingerprint() == instance.fingerprint


def test_dataset_spec_has_no_max_rows():
    """A1：新管线 DatasetSpec 无 max_rows 钳制字段；未知字段拒绝（严格形态）。"""
    assert "max_rows" not in m.DatasetSpec.model_fields
    with pytest.raises(ValidationError):
        m.DatasetSpec.model_validate({"name": "x", "max_rows": 100})


def test_execution_manifest_reproducibility_fields():
    """ExecutionManifest 承载总方案 §12.4 可复现要素全集。"""
    mf = _sample("ExecutionManifest")
    assert mf.partition_algorithm_version == "1"
    assert "orders" in mf.actual_id_ranges
    assert mf.model_fingerprints.schema_fp is not None  # alias "schema"


def test_models_no_db_or_dialect_dependency():
    """模型层不 import 任何数据库驱动/方言模块（AST 扫描 + 子进程导入检查）。

    例外：compat.py 为旧→新适配层，允许 import datagen.model（legacy dataclass），
    但同样禁止数据库驱动。
    """
    import ast

    forbidden_drivers = {"pymysql", "psycopg2", "sqlalchemy"}
    for py_file in (ROOT / "ptde" / "models").glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        bad = roots & forbidden_drivers
        assert not bad, f"{py_file.name} 含数据库驱动依赖: {bad}"
        if py_file.name != "compat.py":
            assert "datagen" not in roots, f"{py_file.name} 不得依赖 legacy datagen"

    code = (
        "import sys; import ptde.models; "
        "bad = [x for x in sys.modules if x.split('.')[0] in "
        "{'pymysql', 'psycopg2', 'sqlalchemy'}]; "
        "assert not bad, bad; print('ok')"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout
