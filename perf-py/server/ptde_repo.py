"""PTDE 数据集生命周期落库层（B7 · T7.2 前置：validator/lifecycle 落库 repo）。

B6 交付的 ``ptde/lifecycle``（DatasetManager 等）为内存模型层（不持久化）；
本模块把 §12.1 三张表接上：

- ``ptde_dataset_definition``：定义（def_key+version 唯一，uk_def_version）；
- ``ptde_dataset_instance``：实例（状态机宿主；transition_log JSON 数组落库；
  ``definition_id`` 列为 BIGINT 外键形态——B6 §4 决策 10 的内存层 str 形态
  ``def_key@version`` 由 repo 转换：DB 存 Definition 行主键，ptde 模型侧仍
  以 ``def_key@version`` 承载，instance.id 以 DB 自增主键的字符串形态承载）；
- ``ptde_validation_report``：五层验证报告（LayerResult 字段形态已对齐，
  R6 §7 口径）；
- ``ptde_execution_manifest``：执行清单（B7 续：READY 门禁条件 7 的
  持久化侧——manifest 此前仅经服务层校验未落库；§12.4 可复现要素落库）。

repo 只做读写映射（对齐 datagen_repo/stress_repo 的平级仓储约定，无统一
基类）；领域语义（状态机/READY 门禁/重复注册显式失败）在 ptde.lifecycle
与服务层，repo 不重复判定。

id 形态映射（沿用 Instance 同款决策，B7 §4 决策 2）：ExecutionManifest.id
以 DB 自增主键字符串形态承载（``str(row.id)``）——init.sql §13.3 无协议
id 列，manifest 身份以行主键承载，链路可溯（instance_id 关联列）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from ptde.models.dataset import (
    DatasetDefinition,
    DatasetInstance,
    LayerKind,
    LayerResult,
    TransitionRecord,
    ValidationReport,
    ValidationStatus,
)
from ptde.models.spec import ExecutionManifest
from server.db import Base, BigIntPk

BigCol = BigInteger  # 非 PK 的 BIGINT 列（与 BigIntPk 方言 variant 同源语义）

__all__ = [
    "PtdeDatasetDefinitionModel",
    "PtdeDatasetInstanceModel",
    "PtdeDatasetRepository",
    "PtdeExecutionManifestModel",
    "PtdeValidationReportModel",
]


class PtdeDatasetDefinitionModel(Base):
    """ptde_dataset_definition：数据集定义（列名对齐 sql/init.sql §13.1）。"""

    __tablename__ = "ptde_dataset_definition"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    def_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    spec_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seed: Mapped[int | None] = mapped_column(BigCol, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PtdeDatasetInstanceModel(Base):
    """ptde_dataset_instance：数据集实例（列名对齐 sql/init.sql §13.2）。"""

    __tablename__ = "ptde_dataset_instance"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    definition_id: Mapped[int] = mapped_column(BigCol, nullable=False)
    definition_version: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_id: Mapped[int | None] = mapped_column(BigCol, nullable=True)
    environment_label: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PLANNED")
    transition_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_rows: Mapped[int | None] = mapped_column(BigCol, nullable=True)
    actual_storage_bytes: Mapped[int | None] = mapped_column(BigCol, nullable=True)
    owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    validation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    legacy_plan_id: Mapped[int | None] = mapped_column(BigCol, nullable=True)
    legacy_task_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PtdeValidationReportModel(Base):
    """ptde_validation_report：五层验证报告（列名对齐 sql/init.sql §13.4）。"""

    __tablename__ = "ptde_validation_report"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(BigCol, nullable=False, index=True)
    layer: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tolerance_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PtdeExecutionManifestModel(Base):
    """ptde_execution_manifest：执行清单（列名对齐 sql/init.sql §13.3）。

    Manifest 四指纹（spec/plan/profile/schema）在 DDL 为平铺四列；
    协议侧 ModelFingerprints 的序列化键为 ``schema``（Python 字段 schema_fp）。
    """

    __tablename__ = "ptde_execution_manifest"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(BigCol, nullable=False, index=True)
    seed: Mapped[int | None] = mapped_column(BigCol, nullable=True)
    generator_version: Mapped[str] = mapped_column(String(32), nullable=False)
    partition_algorithm_version: Mapped[str] = mapped_column(
        String(16), nullable=False)
    spec_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adapter_capability_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    adapter_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    time_inputs_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_id_ranges_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    shard_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _now() -> datetime:
    return datetime.now()


class PtdeDatasetRepository:
    """三表读写仓储（读集合 + 写集合；领域判定在 ptde.lifecycle）。"""

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory
        # 锁序化：sqlite 内存库经 StaticPool 共享单连接，跨线程并发会话的
        # commit/rollback 会互相覆盖（应用线程池 + 工作线程双写场景）；
        # MySQL 池化连接下锁为无竞争快速路径。
        self._lock = threading.Lock()

    # ------------------------------------------------------------ Definition

    def insert_definition(
        self, *, def_key: str, version: str, name: str, spec_json: str,
        spec_fingerprint: str, profile_fingerprint: str | None = None,
        seed: int | None = None, created_by: str | None = None,
    ) -> int:
        """先查后写（uk_def_version 唯一）；重复返回 None 由服务层显式失败。"""
        with self._lock, self._sessions() as s:
            exists = s.scalars(select(PtdeDatasetDefinitionModel).where(
                PtdeDatasetDefinitionModel.def_key == def_key,
                PtdeDatasetDefinitionModel.version == version).limit(1)).first()
            if exists is not None:
                return -1
            row = PtdeDatasetDefinitionModel(
                def_key=def_key, version=version, name=name, spec_json=spec_json,
                spec_fingerprint=spec_fingerprint,
                profile_fingerprint=profile_fingerprint, seed=seed,
                created_by=created_by, create_time=_now(), update_time=_now())
            s.add(row)
            s.commit()
            return int(row.id)

    def find_definition(self, def_key: str, version: str) -> PtdeDatasetDefinitionModel | None:
        with self._lock, self._sessions() as s:
            return s.scalars(select(PtdeDatasetDefinitionModel).where(
                PtdeDatasetDefinitionModel.def_key == def_key,
                PtdeDatasetDefinitionModel.version == version).limit(1)).first()

    def find_definition_by_id(self, row_id: int) -> PtdeDatasetDefinitionModel | None:
        with self._lock, self._sessions() as s:
            return s.get(PtdeDatasetDefinitionModel, row_id)

    def list_definitions(self, limit: int = 50) -> list[PtdeDatasetDefinitionModel]:
        stmt = (select(PtdeDatasetDefinitionModel)
                .order_by(PtdeDatasetDefinitionModel.create_time.desc(),
                          PtdeDatasetDefinitionModel.id.desc())
                .limit(max(1, min(limit, 200))))
        with self._lock, self._sessions() as s:
            return list(s.scalars(stmt))

    def list_versions(self, def_key: str) -> list[str]:
        with self._lock, self._sessions() as s:
            rows = s.scalars(select(PtdeDatasetDefinitionModel).where(
                PtdeDatasetDefinitionModel.def_key == def_key)).all()
        return sorted({r.version for r in rows})

    # ------------------------------------------------------------ Instance

    def insert_instance(
        self, *, definition_row_id: int, definition_version: str,
        environment_label: str, schema_fingerprint: str | None = None,
        status: str = "PLANNED", plan_id: int | None = None,
        legacy_plan_id: int | None = None, legacy_task_ids: list[int] | None = None,
    ) -> int:
        with self._lock, self._sessions() as s:
            row = PtdeDatasetInstanceModel(
                definition_id=definition_row_id, definition_version=definition_version,
                plan_id=plan_id, environment_label=environment_label,
                schema_fingerprint=schema_fingerprint, status=status,
                transition_log="[]",
                legacy_plan_id=legacy_plan_id,
                legacy_task_ids=(json.dumps(legacy_task_ids) if legacy_task_ids else None),
                create_time=_now(), update_time=_now())
            s.add(row)
            s.commit()
            return int(row.id)

    def find_instance(self, instance_id: int) -> PtdeDatasetInstanceModel | None:
        with self._lock, self._sessions() as s:
            return s.get(PtdeDatasetInstanceModel, instance_id)

    def list_instances(self, status: str | None = None,
                       definition_row_id: int | None = None,
                       limit: int = 50) -> list[PtdeDatasetInstanceModel]:
        stmt = (select(PtdeDatasetInstanceModel)
                .order_by(PtdeDatasetInstanceModel.create_time.desc(),
                          PtdeDatasetInstanceModel.id.desc())
                .limit(max(1, min(limit, 200))))
        if status:
            stmt = stmt.where(PtdeDatasetInstanceModel.status == status)
        if definition_row_id is not None:
            stmt = stmt.where(PtdeDatasetInstanceModel.definition_id == definition_row_id)
        with self._lock, self._sessions() as s:
            return list(s.scalars(stmt))

    def list_instances_page(
        self, *, status: str | None = None, before_id: int | None = None,
        limit: int = 50,
    ) -> list[PtdeDatasetInstanceModel]:
        """游标分页读取（keyset：id 降序——自增主键与 create_time 单调一致，
        以行主键为游标跨方言最稳；避免 offset 深翻页与插入导致的漂移）。"""
        stmt = (select(PtdeDatasetInstanceModel)
                .order_by(PtdeDatasetInstanceModel.id.desc())
                .limit(max(1, min(limit, 201))))
        if status:
            stmt = stmt.where(PtdeDatasetInstanceModel.status == status)
        if before_id is not None:
            stmt = stmt.where(PtdeDatasetInstanceModel.id < before_id)
        with self._lock, self._sessions() as s:
            return list(s.scalars(stmt))

    def update_instance_status(self, instance_id: int, status: str,
                               transition_log_json: str | None = None) -> None:
        with self._lock, self._sessions() as s:
            row = s.get(PtdeDatasetInstanceModel, instance_id)
            if row is None:
                return
            row.status = status
            if transition_log_json is not None:
                row.transition_log = transition_log_json
            row.update_time = _now()
            s.commit()

    def update_instance_result(self, instance_id: int, *, actual_rows: int | None = None,
                               actual_storage_bytes: int | None = None,
                               validation_summary: str | None = None,
                               schema_fingerprint: str | None = None) -> None:
        with self._lock, self._sessions() as s:
            row = s.get(PtdeDatasetInstanceModel, instance_id)
            if row is None:
                return
            if actual_rows is not None:
                row.actual_rows = actual_rows
            if actual_storage_bytes is not None:
                row.actual_storage_bytes = actual_storage_bytes
            if validation_summary is not None:
                row.validation_summary = validation_summary
            if schema_fingerprint is not None:
                row.schema_fingerprint = schema_fingerprint
            row.update_time = _now()
            s.commit()

    def update_instance_error(self, instance_id: int, error_msg: str | None) -> None:
        with self._lock, self._sessions() as s:
            row = s.get(PtdeDatasetInstanceModel, instance_id)
            if row is None:
                return
            row.error_msg = error_msg
            row.update_time = _now()
            s.commit()

    def set_owner_token(self, instance_id: int, owner_token: str | None) -> None:
        with self._lock, self._sessions() as s:
            row = s.get(PtdeDatasetInstanceModel, instance_id)
            if row is None:
                return
            row.owner_token = owner_token
            row.update_time = _now()
            s.commit()

    # ------------------------------------------------------------ ValidationReport

    def insert_report(self, *, instance_id: int, layer: str, status: str,
                      metrics_json: str | None = None, tolerance_json: str | None = None,
                      evidence_json: str | None = None) -> int:
        with self._lock, self._sessions() as s:
            row = PtdeValidationReportModel(
                instance_id=instance_id, layer=layer, status=status,
                metrics_json=metrics_json, tolerance_json=tolerance_json,
                evidence_json=evidence_json, create_time=_now())
            s.add(row)
            s.commit()
            return int(row.id)

    def reports_for_instance(self, instance_id: int) -> list[PtdeValidationReportModel]:
        stmt = (select(PtdeValidationReportModel)
                .where(PtdeValidationReportModel.instance_id == instance_id)
                .order_by(PtdeValidationReportModel.id.asc()))
        with self._lock, self._sessions() as s:
            return list(s.scalars(stmt))

    # ------------------------------------------------------------ ptde 模型映射

    def definition_to_model(self, row: PtdeDatasetDefinitionModel) -> DatasetDefinition:
        return DatasetDefinition.model_validate({
            "def_key": row.def_key,
            "version": row.version,
            "name": row.name,
            "spec": json.loads(row.spec_json),
            "spec_fingerprint": row.spec_fingerprint,
            "created_by": row.created_by,
        })

    def instance_to_model(
        self, row: PtdeDatasetInstanceModel,
        def_key: str | None = None,
    ) -> DatasetInstance:
        """行 → ptde DatasetInstance（id=str(行主键)；definition_id=def_key@version）。"""
        if def_key is None:
            definition = self.find_definition_by_id(row.definition_id)
            def_key = definition.def_key if definition is not None else ""
        log_entries = json.loads(row.transition_log) if row.transition_log else []
        return DatasetInstance.model_validate({
            "id": str(row.id),
            "definition_id": f"{def_key}@{row.definition_version}",
            "definition_version": row.definition_version,
            "plan_id": str(row.plan_id) if row.plan_id is not None else None,
            "target_environment": {"environment_id": row.environment_label},
            "status": row.status,
            "transition_log": log_entries,
            "actual_rows": row.actual_rows,
            "actual_storage_bytes": row.actual_storage_bytes,
            "validation_summary": (
                json.loads(row.validation_summary) if row.validation_summary else None),
            "owner_token": row.owner_token,
            "error": row.error_msg,
        })

    def transition_log_to_json(self, instance: DatasetInstance) -> str:
        return json.dumps(
            [json.loads(r.model_dump_json()) if isinstance(r, TransitionRecord) else r
             for r in instance.transition_log],
            ensure_ascii=False)

    def report_to_model(self, instance_id: str,
                        rows: list[PtdeValidationReportModel]) -> ValidationReport:
        """报告行 → ptde ValidationReport（按记录序聚合五层）。"""
        layers = [
            LayerResult.model_validate({
                "layer": r.layer,
                "status": r.status,
                "metrics": json.loads(r.metrics_json) if r.metrics_json else {},
                "tolerance": json.loads(r.tolerance_json) if r.tolerance_json else None,
                "evidence": json.loads(r.evidence_json) if r.evidence_json else [],
            })
            for r in rows
        ]
        return ValidationReport(instance_id=instance_id, layers=layers)

    def report_from_layer(self, instance_row_id: int, layer: LayerResult) -> int:
        """LayerResult → 报告行（metrics/tolerance/evidence JSON 序列化）。"""
        return self.insert_report(
            instance_id=instance_row_id,
            layer=layer.layer.value if isinstance(layer.layer, LayerKind) else layer.layer,
            status=layer.status.value if isinstance(layer.status, ValidationStatus) else layer.status,
            metrics_json=json.dumps(layer.metrics, ensure_ascii=False),
            tolerance_json=(json.dumps(layer.tolerance, ensure_ascii=False)
                            if layer.tolerance is not None else None),
            evidence_json=json.dumps(layer.evidence, ensure_ascii=False),
        )

    # ------------------------------------------------------------ ExecutionManifest（B7 续）

    def insert_manifest(self, instance_row_id: int, manifest: ExecutionManifest) -> int:
        """ExecutionManifest → manifest 行（§12.4 可复现要素落库；JSON 列序列化）。

        spec_fingerprint 为 NOT NULL 列：模型缺项时落空串哨兵（READY 门禁
        条件 7 会如实拒绝，行仍留痕——不因缺项阻断可追溯性）。
        """
        fps = manifest.model_fingerprints
        row = PtdeExecutionManifestModel(
            instance_id=instance_row_id,
            seed=manifest.seed,
            generator_version=manifest.generator_version,
            partition_algorithm_version=manifest.partition_algorithm_version,
            spec_fingerprint=fps.spec or "",
            plan_fingerprint=fps.plan,
            profile_fingerprint=fps.profile,
            schema_fingerprint=fps.schema_fp,
            adapter_capability_json=json.dumps(
                manifest.adapter_capability_snapshot, ensure_ascii=False),
            adapter_version=manifest.adapter_version,
            time_inputs_json=json.dumps(manifest.time_inputs, ensure_ascii=False),
            actual_id_ranges_json=json.dumps(
                {k: v.model_dump(mode="json") for k, v in
                 manifest.actual_id_ranges.items()},
                ensure_ascii=False),
            shard_summary_json=json.dumps(
                [s.model_dump(mode="json") for s in manifest.shard_summaries],
                ensure_ascii=False),
            start_time=manifest.start_time,
            end_time=manifest.end_time,
            create_time=_now(),
        )
        with self._lock, self._sessions() as s:
            s.add(row)
            s.commit()
            return int(row.id)

    def manifests_for_instance(
            self, instance_row_id: int) -> list[PtdeExecutionManifestModel]:
        stmt = (select(PtdeExecutionManifestModel)
                .where(PtdeExecutionManifestModel.instance_id == instance_row_id)
                .order_by(PtdeExecutionManifestModel.id.asc()))
        with self._lock, self._sessions() as s:
            return list(s.scalars(stmt))

    def manifest_to_model(self, row: PtdeExecutionManifestModel) -> ExecutionManifest:
        """manifest 行 → ptde ExecutionManifest（id=str(行主键)，Instance 同款）。"""
        return ExecutionManifest.model_validate({
            "id": str(row.id),
            "instance_id": str(row.instance_id),
            "seed": row.seed,
            "generator_version": row.generator_version,
            "partition_algorithm_version": row.partition_algorithm_version,
            "model_fingerprints": {
                "spec": row.spec_fingerprint or None,
                "plan": row.plan_fingerprint,
                "profile": row.profile_fingerprint,
                "schema": row.schema_fingerprint,
            },
            "adapter_capability_snapshot": (
                json.loads(row.adapter_capability_json)
                if row.adapter_capability_json else {}),
            "adapter_version": row.adapter_version,
            "time_inputs": json.loads(row.time_inputs_json) if row.time_inputs_json else {},
            "actual_id_ranges": json.loads(row.actual_id_ranges_json)
            if row.actual_id_ranges_json else {},
            "shard_summaries": json.loads(row.shard_summary_json)
            if row.shard_summary_json else [],
            "start_time": row.start_time,
            "end_time": row.end_time,
        })

    @staticmethod
    def manifest_summary(manifest: ExecutionManifest) -> dict:
        """ExecutionManifest → camelCase 摘要 dict（API 展开形态，§15.2）。"""
        fps = manifest.model_fingerprints
        return {
            "id": manifest.id,
            "instanceId": manifest.instance_id,
            "seed": manifest.seed,
            "generatorVersion": manifest.generator_version,
            "partitionAlgorithmVersion": manifest.partition_algorithm_version,
            "fingerprints": {
                "spec": fps.spec, "plan": fps.plan,
                "profile": fps.profile, "schema": fps.schema_fp,
            },
            "adapterCapabilitySnapshot": manifest.adapter_capability_snapshot,
            "adapterVersion": manifest.adapter_version,
            "timeInputs": manifest.time_inputs,
            "actualIdRanges": {
                k: v.model_dump(mode="json")
                for k, v in manifest.actual_id_ranges.items()},
            "shardSummaries": [s.model_dump(mode="json")
                               for s in manifest.shard_summaries],
            "startTime": (manifest.start_time.isoformat(timespec="seconds")
                          if manifest.start_time else None),
            "endTime": (manifest.end_time.isoformat(timespec="seconds")
                        if manifest.end_time else None),
        }
