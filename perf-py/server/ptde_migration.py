"""A4a legacy 存量迁移（T7.8，实施方案 §12.5）：legacy 三表 → ptde_* 幂等可重放。

兼容用途：存量 legacy 三表（data_gen_task/data_gen_plan/semantic_hint）→
ptde_* 的迁移入口，由生产 CLI ``perf ptde migrate-legacy`` 调用；T7.9 退役
清单未将其列入删除范围。
预计下线条件：所有存量库完成迁移、旧三表归档清退且 CLI 迁移命令下线后移除。

映射（§12.5 原文口径）：
- ``data_gen_task`` → ``ptde_dataset_instance``（历史灌数登记为历史 Instance，
  ``legacy_task_ids=[task.id]``；definition 按需从 ``tables_json``/单表列反解
  生成为 ``legacy-task-{id}``@version='legacy'——无法反解的存 ``error_msg``
  说明并保留原始 JSON 于归档表）；
- ``data_gen_plan`` → ``ptde_dataset_definition``（version='legacy'）+
  ``ptde_dataset_instance``（``legacy_plan_id``；``endpoint_json/stress_json``
  原始引用保留于 ``validation_summary``）；
- ``semantic_hint`` → ``ptde_finding``（``legacy_hint_id``；hint_type →
  finding_type 映射；adopted/dismissed 状态直迁）；
- 迁移后旧三表只读归档（回溯期保留由环境侧收权落地，本脚本不越权 DDL）。

脚本要求（§12.5）：先查后写幂等（以 ``legacy_*`` 字段为去重键）；可重放；
迁移前后行数核对报告；失败即停不留半态（单事务，异常即整体回滚）。

状态映射口径（历史登记，非活体流转——**不经** ``state_machine.transition()``，
transition_log 以 PLANNED→目标状态单条记录承载审计，operator=legacy-migration；
伪造多跳历史链反而失真）：
- task：finished→READY（actual_rows=inserted）/ failed→FAILED（error_msg 透传）/
  pending|running→CANCELLED（未执行完成，无灌数结果可核对）；未知状态失败即停。
- plan：executed→READY / failed→FAILED / pending|adopted|discarded→CANCELLED。
- hint 采纳状态：pending/adopted/dismissed 直迁；去重键 ``legacy_hint_id``
  （uk_finding 幂等键不含 legacy 来源，命中即跳过不覆盖——直迁状态不被重放回滚）。

价值侧取值（hint_type → finding_type）：``value``（魔法值候选）→ ``enum``、
``relation``（关联关系）→ ``relation``——对齐 ptde 分析器既有 finding_type 词汇；
证据三态缺省 ``INFERRED``（semantic_hint 无三态证据模型，不伪装 CONFIRMED）。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ptde.analyzer.finding_repo import PtdeFindingModel
from ptde.models.dataset import DatasetState, TransitionRecord
from ptde.models.spec import DatasetSpec
from ptde_common.model_base import canonical_json, sha256_hex
from server.datagen_repo import DataGenPlanModel, DataGenTaskModel, SemanticHintModel
from server.ptde_repo import PtdeDatasetDefinitionModel, PtdeDatasetInstanceModel

__all__ = ["MigrationError", "run_legacy_migration"]

LEGACY_VERSION = "legacy"
MIGRATION_OPERATOR = "legacy-migration"
ANALYZER_VERSION = "legacy-migration"
STATUS_THREE_DEFAULT = "INFERRED"

TASK_DEF_PREFIX = "legacy-task-"
PLAN_DEF_PREFIX = "legacy-plan-"

# 实施方案 §12.5：data_gen_task 历史灌数按实际状态映射（未知状态失败即停）
TASK_STATUS_MAP = {
    "finished": "READY",
    "failed": "FAILED",
    "pending": "CANCELLED",
    "running": "CANCELLED",
}
PLAN_STATUS_MAP = {
    "executed": "READY",
    "failed": "FAILED",
    "pending": "CANCELLED",
    "adopted": "CANCELLED",
    "discarded": "CANCELLED",
}
# §12.5：hint_type → finding_type 映射表；采纳状态直迁
HINT_TYPE_MAP = {"value": "enum", "relation": "relation"}
ADOPT_STATUS_MAP = {"pending": "pending", "adopted": "adopted", "dismissed": "dismissed"}


class MigrationError(RuntimeError):
    """迁移失败即停（§12.5：不留半态——调用方事务整体回滚）。"""


def _now() -> datetime:
    return datetime.now()


def _map_status(source: str, legacy_status: str | None, mapping: dict[str, str]) -> str:
    target = mapping.get(str(legacy_status or "").strip())
    if target is None:
        raise MigrationError(
            f"{source} status={legacy_status!r} 不在既定映射内（失败即停，§12.5）")
    return target


def _transition_log_json(to_state: str, provenance: str) -> str:
    """迁移审计记录：PLANNED→目标状态单条，operator/reason 承载映射出处。"""
    record = TransitionRecord(
        from_state=DatasetState.PLANNED,
        to_state=DatasetState(to_state),
        operator=MIGRATION_OPERATOR,
        reason=f"A4a 存量迁移（实施方案 §12.5）：{provenance}",
        occurred_at=_now(),
    )
    return json.dumps([record.model_dump(mode="json")], ensure_ascii=False)


def _rows_from_tables_json(tables_json: str | None, key_table: str) -> tuple[dict[str, int], bool]:
    """表清单 JSON 反解为 {表名: 行数}（>0 才入 spec——DatasetSpec 行数须为正）。

    返回 (rows, parse_failed)：parse_failed 仅在「内容存在但非法 JSON/非数组」
    时为 True（无法反解 → error_msg 说明）；空/None 属正常空态不算失败。
    """
    if not tables_json or not tables_json.strip():
        return {}, False
    try:
        arr = json.loads(tables_json)
    except (TypeError, ValueError):
        return {}, True
    if not isinstance(arr, list):
        return {}, True
    rows: dict[str, int] = {}
    for node in arr:
        if not isinstance(node, dict) or not node.get(key_table):
            continue
        count = node.get("rowCount")
        if isinstance(count, int) and count > 0:
            rows[str(node[key_table])] = count
    return rows, False


def _spec_draft(name: str, rows: dict[str, int], db_url: str | None) -> DatasetSpec:
    """DatasetSpec 草案（§12.5：tables_json 反解为草案，反解不出存说明）。"""
    return DatasetSpec.model_validate({
        "name": name,
        "target_database": db_url,
        "targets": {"rows": rows},
    })


def _ensure_definition(
    s: Session, *, def_key: str, name: str,
    rows: dict[str, int], db_url: str | None,
) -> tuple[int, bool]:
    """先查后写（uk_def_version；def_key 确定性派生自 legacy 主键 → 重放稳定）。"""
    exists = s.scalars(select(PtdeDatasetDefinitionModel).where(
        PtdeDatasetDefinitionModel.def_key == def_key,
        PtdeDatasetDefinitionModel.version == LEGACY_VERSION,
    ).limit(1)).first()
    if exists is not None:
        return int(exists.id), False
    spec = _spec_draft(name or def_key, rows, db_url)
    row = PtdeDatasetDefinitionModel(
        def_key=def_key, version=LEGACY_VERSION, name=name or def_key,
        spec_json=spec.model_dump_json(), spec_fingerprint=spec.compute_fingerprint(),
        created_by=MIGRATION_OPERATOR, create_time=_now(), update_time=_now())
    s.add(row)
    s.flush()
    return int(row.id), True


def _migrated_task_ids(s: Session) -> set[int]:
    """已迁移 task 集合（去重键：ptde_dataset_instance.legacy_task_ids JSON）。"""
    rows = s.scalars(select(PtdeDatasetInstanceModel).where(
        PtdeDatasetInstanceModel.legacy_task_ids.is_not(None))).all()
    ids: set[int] = set()
    for row in rows:
        try:
            parsed = json.loads(row.legacy_task_ids)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, list):
            ids.update(int(i) for i in parsed if isinstance(i, int))
    return ids


def _migrated_plan_ids(s: Session) -> set[int]:
    """已迁移 plan 集合（去重键：ptde_dataset_instance.legacy_plan_id）。"""
    rows = s.scalars(select(PtdeDatasetInstanceModel.legacy_plan_id).where(
        PtdeDatasetInstanceModel.legacy_plan_id.is_not(None))).all()
    return {int(v) for v in rows}


def _json_or_raw(text: str | None) -> str | dict | list | None:
    """原始引用保留：可解析则结构化存（不丢信息），否则原样保留。"""
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _migrate_plans(s: Session) -> dict:
    """data_gen_plan → definition(version='legacy') + instance(legacy_plan_id)。"""
    plans = list(s.scalars(select(DataGenPlanModel).order_by(DataGenPlanModel.id)))
    migrated = _migrated_plan_ids(s)
    definitions = instances = skipped = 0
    for plan in plans:
        if plan.id in migrated:
            skipped += 1
            continue
        rows, parse_failed = _rows_from_tables_json(plan.tables_json, "table")
        status = _map_status("data_gen_plan", plan.status, PLAN_STATUS_MAP)
        def_id, def_created = _ensure_definition(
            s, def_key=f"{PLAN_DEF_PREFIX}{plan.id}", name=plan.plan_name,
            rows=rows, db_url=plan.db_url)
        definitions += int(def_created)
        note = (f"legacy 方案表清单 JSON 反解失败，原始 JSON 保留于归档表 "
                f"data_gen_plan(id={plan.id})") if parse_failed else None
        error_msg = (f"legacy 方案执行失败（迁移登记，原始归档 "
                     f"data_gen_plan(id={plan.id})）") if status == "FAILED" else note
        prov = {
            "legacy": {
                "source": "data_gen_plan",
                "legacyPlanId": plan.id,
                "legacyStatus": plan.status,
                "taskId": plan.task_id,
                "connId": plan.conn_id,
                # endpoint_json/stress_json 原始引用（§12.5：保留原始引用）
                "endpointJson": _json_or_raw(plan.endpoint_json),
                "stressJson": _json_or_raw(plan.stress_json),
                "warningsJson": _json_or_raw(plan.warnings_json),
            },
        }
        s.add(PtdeDatasetInstanceModel(
            definition_id=def_id, definition_version=LEGACY_VERSION,
            plan_id=None, environment_label=plan.db_url or "unknown",
            status=status,
            transition_log=_transition_log_json(
                status, f"legacy data_gen_plan(id={plan.id}) status={plan.status}"),
            validation_summary=json.dumps(prov, ensure_ascii=False),
            error_msg=error_msg, legacy_plan_id=plan.id,
            create_time=_now(), update_time=_now()))
        s.flush()
        instances += 1
    return {"total": len(plans), "definitions_created": definitions,
            "instances_created": instances, "skipped": skipped}


def _migrate_tasks(s: Session) -> dict:
    """data_gen_task → instance(legacy_task_ids=[task.id]) + 按需 definition。"""
    tasks = list(s.scalars(select(DataGenTaskModel).order_by(DataGenTaskModel.id)))
    migrated = _migrated_task_ids(s)
    definitions = instances = skipped = 0
    for task in tasks:
        if task.id in migrated:
            skipped += 1
            continue
        rows, parse_failed = _rows_from_tables_json(task.tables_json, "tableName")
        if not rows:
            single = task.row_count if task.row_count > 0 else (task.total_rows or 0)
            if task.table_name and single > 0:
                rows = {task.table_name: single}
        status = _map_status("data_gen_task", task.status, TASK_STATUS_MAP)
        def_id, def_created = _ensure_definition(
            s, def_key=f"{TASK_DEF_PREFIX}{task.id}", name=task.task_name,
            rows=rows, db_url=task.db_url or None)
        definitions += int(def_created)
        # §12.5：无法反解的存 error_msg 说明并保留原始 JSON（归档表承载原始行）
        notes: list[str] = []
        if parse_failed and not rows:
            notes.append(f"legacy 表清单 JSON 反解失败且无可用单表行数，原始 JSON "
                         f"保留于归档表 data_gen_task(id={task.id})")
        if status == "FAILED":
            notes.append(task.error_msg or "legacy 任务失败（无 error_msg 留痕）")
        elif status == "CANCELLED":
            notes.append(f"legacy 存量迁移登记：任务未执行完成（status={task.status}），"
                         "无灌数结果可核对")
        prov = {
            "legacy": {
                "source": "data_gen_task",
                "legacyTaskId": task.id,
                "legacyStatus": task.status,
                "inserted": task.inserted,
                "costMs": task.cost_ms,
            },
        }
        s.add(PtdeDatasetInstanceModel(
            definition_id=def_id, definition_version=LEGACY_VERSION,
            plan_id=None, environment_label=task.db_url or "unknown",
            status=status,
            transition_log=_transition_log_json(
                status, f"legacy data_gen_task(id={task.id}) status={task.status}"),
            actual_rows=(task.inserted if status == "READY" else None),
            validation_summary=json.dumps(prov, ensure_ascii=False),
            error_msg="；".join(notes) if notes else None,
            legacy_task_ids=json.dumps([task.id]),
            create_time=_now(), update_time=_now()))
        s.flush()
        instances += 1
    return {"total": len(tasks), "definitions_created": definitions,
            "instances_created": instances, "skipped": skipped}


def _hint_value(hint) -> dict:
    """hint → finding value（结构化；values_json 不可解析降级空数组，原始行归档保留）。"""
    base = {"table": hint.table_name, "source": hint.source}
    if hint.hint_type == "value":
        try:
            values = json.loads(hint.values_json) if hint.values_json else []
        except (TypeError, ValueError):
            values = []
        return {**base, "column": hint.column_name, "values": values if isinstance(values, list) else []}
    return {**base, "column": hint.column_name,
            "parentTable": hint.parent_table, "parentColumn": hint.parent_column}


def _migrate_hints(s: Session) -> dict:
    """semantic_hint → ptde_finding（legacy_hint_id 去重；adopt_status 直迁）。"""
    hints = list(s.scalars(select(SemanticHintModel).order_by(SemanticHintModel.id)))
    migrated = set(s.scalars(select(PtdeFindingModel.legacy_hint_id).where(
        PtdeFindingModel.legacy_hint_id.is_not(None))).all())
    created = skipped = 0
    for hint in hints:
        if hint.id in migrated:
            skipped += 1
            continue
        finding_type = HINT_TYPE_MAP.get(hint.hint_type)
        if finding_type is None:
            raise MigrationError(
                f"semantic_hint(id={hint.id}) hint_type={hint.hint_type!r} "
                "不在映射表内（失败即停，§12.5）")
        adopt_status = ADOPT_STATUS_MAP.get(hint.status)
        if adopt_status is None:
            raise MigrationError(
                f"semantic_hint(id={hint.id}) status={hint.status!r} "
                "不在直迁状态集内（失败即停，§12.5）")
        value = _hint_value(hint)
        # uk_finding 兜底：同 (repo_id, type, source_object, fingerprint) 的
        # 重复 hint 视为已迁移（跳过），避免唯一键冲突中断整批
        fingerprint = sha256_hex(canonical_json(value))
        dup = s.scalars(select(PtdeFindingModel).where(
            PtdeFindingModel.repo_id == hint.repo_id,
            PtdeFindingModel.finding_type == finding_type,
            PtdeFindingModel.source_object == hint.table_name,
            PtdeFindingModel.value_fingerprint == fingerprint,
        ).limit(1)).first()
        if dup is not None:
            skipped += 1
            continue
        s.add(PtdeFindingModel(
            repo_id=hint.repo_id, finding_type=finding_type,
            value_json=json.dumps(value, ensure_ascii=False, sort_keys=True),
            status_three=STATUS_THREE_DEFAULT, source_object=hint.table_name,
            evidence_json=hint.source_refs, confidence=hint.confidence,
            analyzer_version=ANALYZER_VERSION, adopt_status=adopt_status,
            legacy_hint_id=hint.id, value_fingerprint=fingerprint,
            create_time=_now(), update_time=_now()))
        s.flush()
        created += 1
    return {"total": len(hints), "findings_created": created, "skipped": skipped}


def _count(s: Session, model) -> int:
    return int(s.scalar(select(func.count()).select_from(model)))


def run_legacy_migration(session_factory) -> dict:
    """执行存量迁移（单事务：失败即停不留半态；返回行数核对报告）。

    幂等可重放：以 legacy_* 字段为去重键先查后写——重放时已迁移行全部
    skipped，目标表行数不变。核对不通过（after != before + created）时
    抛 MigrationError 整体回滚，不提交任何变更。
    """
    with session_factory() as s:
        before = {
            "ptde_dataset_definition": _count(s, PtdeDatasetDefinitionModel),
            "ptde_dataset_instance": _count(s, PtdeDatasetInstanceModel),
            "ptde_finding": _count(s, PtdeFindingModel),
        }
        report = {
            "migration": "legacy-to-ptde",
            "startedAt": _now().isoformat(timespec="seconds"),
            "sources": {
                "data_gen_plan": _migrate_plans(s),
                "data_gen_task": _migrate_tasks(s),
                "semantic_hint": _migrate_hints(s),
            },
        }
        # 源表行数核对（迁移前后源表只读，行数应不变）
        source_counts = {
            "data_gen_plan": _count(s, DataGenPlanModel),
            "data_gen_task": _count(s, DataGenTaskModel),
            "semantic_hint": _count(s, SemanticHintModel),
        }
        report["sources"]["data_gen_plan"]["sourceRows"] = source_counts["data_gen_plan"]
        report["sources"]["data_gen_task"]["sourceRows"] = source_counts["data_gen_task"]
        report["sources"]["semantic_hint"]["sourceRows"] = source_counts["semantic_hint"]

        after = {
            "ptde_dataset_definition": _count(s, PtdeDatasetDefinitionModel),
            "ptde_dataset_instance": _count(s, PtdeDatasetInstanceModel),
            "ptde_finding": _count(s, PtdeFindingModel),
        }
        expected = {
            "ptde_dataset_definition": (
                before["ptde_dataset_definition"]
                + report["sources"]["data_gen_plan"]["definitions_created"]
                + report["sources"]["data_gen_task"]["definitions_created"]),
            "ptde_dataset_instance": (
                before["ptde_dataset_instance"]
                + report["sources"]["data_gen_plan"]["instances_created"]
                + report["sources"]["data_gen_task"]["instances_created"]),
            "ptde_finding": (
                before["ptde_finding"]
                + report["sources"]["semantic_hint"]["findings_created"]),
        }
        targets = {}
        verified = True
        for table, count_after in after.items():
            ok = count_after == expected[table]
            verified = verified and ok
            targets[table] = {
                "before": before[table], "after": count_after,
                "created": expected[table] - before[table], "verified": ok,
            }
        report["targets"] = targets
        if not verified:
            raise MigrationError(
                f"行数核对不通过（§12.5 失败即停）：{targets}")
        report["verified"] = True
        s.commit()
    return report
