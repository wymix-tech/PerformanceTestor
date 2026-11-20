"""server/ptde_migration 单测（T7.8，实施方案 §12.5）。

覆盖：legacy 三表 → ptde_* 映射、幂等可重放（重放全 skipped）、失败即停
（单事务回滚不留半态）、行数核对报告形状、transition_log 迁移审计、
原始 JSON 保留口径（unparseable → error_msg 说明 + 归档表承载原始行）。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import server.datagen_repo  # noqa: F401  注册 legacy 模型
import server.ptde_repo  # noqa: F401  注册 ptde 模型
from ptde.analyzer.finding_repo import FindingBase, PtdeFindingModel
from ptde_common.model_base import canonical_json, sha256_hex
from server.datagen_repo import DataGenPlanModel, DataGenTaskModel, SemanticHintModel
from server.db import Base, make_engine
from server.ptde_migration import MigrationError, run_legacy_migration
from server.ptde_repo import PtdeDatasetDefinitionModel, PtdeDatasetInstanceModel


def _factory():
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    FindingBase.metadata.create_all(factory.kw["bind"])
    return factory


def _seed(factory, *rows) -> None:
    with factory() as s:
        for row in rows:
            s.add(row)
        s.commit()


def _task(task_id: int | None = None, *, status: str = "finished",
          row_count: int = 100, inserted: int = 100, tables_json: str | None = None,
          table_name: str = "orders", error_msg: str | None = None) -> DataGenTaskModel:
    row = DataGenTaskModel(
        task_name=f"灌数据-{table_name}", db_url="jdbc:mysql://legacy/db",
        table_name=table_name, row_count=row_count, total_rows=row_count,
        tables_json=tables_json, status=status, inserted=inserted,
        cost_ms=1234, error_msg=error_msg, create_time=datetime(2026, 1, 1))
    if task_id is not None:
        row.id = task_id
    return row


def _plan(plan_id: int | None = None, *, status: str = "executed",
          tables_json: str | None = None) -> DataGenPlanModel:
    row = DataGenPlanModel(
        plan_name="下单接口方案", endpoint_json='[{"httpMethod":"POST","path":"/orders","weight":1.0}]',
        stress_json='{"concurrency":50,"durationSec":300}', db_url="jdbc:mysql://legacy/db",
        tables_json=tables_json, status=status, task_id=None,
        create_time=datetime(2026, 1, 1), update_time=datetime(2026, 1, 1))
    if plan_id is not None:
        row.id = plan_id
    return row


def _hint(hint_id: int | None = None, *, hint_type: str = "value",
          status: str = "pending", values_json: str | None = None) -> SemanticHintModel:
    row = SemanticHintModel(
        repo_id=7, hint_type=hint_type, table_name="orders",
        column_name="status" if hint_type == "value" else "user_id",
        values_json=values_json if hint_type == "value" else None,
        parent_table="users" if hint_type == "relation" else "",
        parent_column="id" if hint_type == "relation" else "",
        source_refs='["OrderService.java:42"]', source="literal",
        confidence=0.9, status=status, create_time=datetime(2026, 1, 1))
    if hint_id is not None:
        row.id = hint_id
    return row


def _definitions(factory) -> list[PtdeDatasetDefinitionModel]:
    with factory() as s:
        return list(s.scalars(select(PtdeDatasetDefinitionModel)).all())


def _instances(factory) -> list[PtdeDatasetInstanceModel]:
    with factory() as s:
        return list(s.scalars(select(PtdeDatasetInstanceModel)).all())


def _findings(factory) -> list[PtdeFindingModel]:
    with factory() as s:
        return list(s.scalars(select(PtdeFindingModel)).all())


# ---------------------------------------------------------------- 任务映射


def test_task_status_mapping_and_results():
    factory = _factory()
    _seed(factory,
          _task(11, status="finished", row_count=500, inserted=480),
          _task(12, status="failed", error_msg="duplicate key"),
          _task(13, status="pending"),
          _task(14, status="running"))
    report = run_legacy_migration(factory)

    instances = {int(json.loads(i.legacy_task_ids)[0]): i for i in _instances(factory)}
    assert set(instances) == {11, 12, 13, 14}
    assert instances[11].status == "READY"
    assert instances[11].actual_rows == 480
    assert instances[11].error_msg is None
    assert instances[12].status == "FAILED"
    assert instances[12].error_msg == "duplicate key"
    assert instances[13].status == "CANCELLED"
    assert "未执行完成" in instances[13].error_msg
    assert instances[14].status == "CANCELLED"
    assert report["sources"]["data_gen_task"]["instances_created"] == 4


def test_task_multi_table_tables_json_parsed_into_spec_rows():
    factory = _factory()
    _seed(factory, _task(21, tables_json=json.dumps([
        {"tableName": "orders", "rowCount": 300},
        {"tableName": "order_items", "rowCount": 900},
    ])))
    run_legacy_migration(factory)

    (definition,) = _definitions(factory)
    spec = json.loads(definition.spec_json)
    assert spec["targets"]["rows"] == {"orders": 300, "order_items": 900}
    assert definition.version == "legacy"
    assert definition.def_key == "legacy-task-21"


def test_unparseable_tables_json_error_msg_note_and_empty_rows_spec():
    factory = _factory()
    _seed(factory, _task(31, status="pending", tables_json="not-json{",
                         table_name="", row_count=0, inserted=0))
    run_legacy_migration(factory)

    (instance,) = _instances(factory)
    assert instance.status == "CANCELLED"
    assert "反解失败" in instance.error_msg
    assert "data_gen_task(id=31)" in instance.error_msg
    (definition,) = _definitions(factory)
    assert json.loads(definition.spec_json)["targets"]["rows"] == {}


# ---------------------------------------------------------------- 方案映射


def test_plan_migration_definition_instance_and_raw_refs():
    factory = _factory()
    _seed(factory, _plan(51, status="executed", tables_json=json.dumps(
        [{"table": "orders", "rowCount": 200, "access": "WRITE"}])))
    report = run_legacy_migration(factory)

    (definition,) = _definitions(factory)
    assert definition.def_key == "legacy-plan-51"
    assert definition.version == "legacy"
    spec = json.loads(definition.spec_json)
    assert spec["targets"]["rows"] == {"orders": 200}

    (instance,) = _instances(factory)
    assert instance.status == "READY"
    assert instance.legacy_plan_id == 51
    prov = json.loads(instance.validation_summary)["legacy"]
    assert prov["endpointJson"] == [{"httpMethod": "POST", "path": "/orders", "weight": 1.0}]
    assert prov["stressJson"] == {"concurrency": 50, "durationSec": 300}
    assert report["sources"]["data_gen_plan"]["sourceRows"] == 1


def test_plan_unexecuted_status_maps_cancelled():
    factory = _factory()
    _seed(factory, _plan(52, status="adopted"))
    run_legacy_migration(factory)
    (instance,) = _instances(factory)
    assert instance.status == "CANCELLED"
    assert instance.error_msg is None


# ---------------------------------------------------------------- 提示映射


def test_hint_mapping_value_relation_and_adopt_status():
    factory = _factory()
    _seed(factory,
          _hint(61, hint_type="value", status="adopted", values_json='["PAID","VOID"]'),
          _hint(62, hint_type="relation", status="dismissed"))
    report = run_legacy_migration(factory)

    findings = {f.legacy_hint_id: f for f in _findings(factory)}
    value = findings[61]
    assert value.finding_type == "enum"
    assert value.adopt_status == "adopted"
    assert json.loads(value.value_json) == {
        "table": "orders", "column": "status", "source": "literal",
        "values": ["PAID", "VOID"]}
    relation = findings[62]
    assert relation.finding_type == "relation"
    assert relation.adopt_status == "dismissed"
    assert json.loads(relation.value_json)["parentTable"] == "users"
    assert value.value_fingerprint == sha256_hex(canonical_json(
        json.loads(value.value_json)))
    assert value.analyzer_version == "legacy-migration"
    assert value.status_three == "INFERRED"
    assert report["sources"]["semantic_hint"]["findings_created"] == 2


def test_hint_unknown_type_fails_and_rolls_back():
    factory = _factory()
    _seed(factory, _hint(63, hint_type="weird"), _task(41))
    with pytest.raises(MigrationError, match="hint_type"):
        run_legacy_migration(factory)
    # 失败即停不留半态：task 41 未落任何目标行
    assert _instances(factory) == []
    assert _definitions(factory) == []
    assert _findings(factory) == []


# ---------------------------------------------------------------- 幂等与核对


def test_replay_idempotent_all_skipped_and_counts_unchanged():
    factory = _factory()
    _seed(factory,
          _task(71, status="finished"),
          _plan(72, status="executed", tables_json='[{"table":"orders","rowCount":10}]'),
          _hint(73, hint_type="value", values_json='["A"]'))
    first = run_legacy_migration(factory)
    assert first["verified"] is True
    counts_first = {k: v["after"] for k, v in first["targets"].items()}
    assert counts_first == {"ptde_dataset_definition": 2,
                            "ptde_dataset_instance": 2, "ptde_finding": 1}

    second = run_legacy_migration(factory)
    assert second["sources"]["data_gen_task"]["skipped"] == 1
    assert second["sources"]["data_gen_task"]["instances_created"] == 0
    assert second["sources"]["data_gen_plan"]["skipped"] == 1
    assert second["sources"]["semantic_hint"]["skipped"] == 1
    assert {k: v["after"] for k, v in second["targets"].items()} == counts_first
    assert second["verified"] is True


def test_report_shape_and_count_verification():
    factory = _factory()
    _seed(factory, _task(81), _plan(82), _hint(83, hint_type="relation"))
    report = run_legacy_migration(factory)

    assert set(report) == {"migration", "startedAt", "sources", "targets", "verified"}
    assert set(report["sources"]) == {"data_gen_plan", "data_gen_task", "semantic_hint"}
    assert report["sources"]["data_gen_task"]["sourceRows"] == 1
    for _table, target in report["targets"].items():
        assert set(target) == {"before", "after", "created", "verified"}
        assert target["after"] == target["before"] + target["created"]
        assert target["verified"] is True
    assert report["targets"]["ptde_finding"]["after"] == 1


def test_unknown_task_status_fails_and_rolls_back():
    factory = _factory()
    _seed(factory, _task(91, status="finished"),
          _task(92, status="weird"))
    with pytest.raises(MigrationError, match="data_gen_task status='weird'"):
        run_legacy_migration(factory)
    assert _instances(factory) == []
    assert _definitions(factory) == []


# ---------------------------------------------------------------- 审计


def test_transition_log_records_migration_provenance():
    factory = _factory()
    _seed(factory, _task(101, status="finished"))
    run_legacy_migration(factory)

    (instance,) = _instances(factory)
    (record,) = json.loads(instance.transition_log)
    assert record["from_state"] == "PLANNED"
    assert record["to_state"] == "READY"
    assert record["operator"] == "legacy-migration"
    assert "data_gen_task(id=101)" in record["reason"]
    assert "status=finished" in record["reason"]
    assert record["occurred_at"]
