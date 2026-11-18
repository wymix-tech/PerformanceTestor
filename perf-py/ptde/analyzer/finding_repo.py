"""ptde_finding 落库 + 采纳流对接（T1.8）。

复用 `semantic_hint` 的工作流模式（server/datagen_repo.py hint_upsert 先查后插，
对齐 uk_hint ON DUPLICATE KEY UPDATE 语义）：
- 幂等键 `uk_finding (repo_id, finding_type, source_object, value_fingerprint)`；
- 命中时更新 value_json/evidence_json/confidence/status_three/analyzer_version，
  **adopt_status 保留原值**（用户已采纳/忽略的发现不被分析重跑回滚）；
- 采纳流转 pending → adopted / dismissed（对齐 semantic_hint status 语义）。

value_fingerprint = sha256(canonical_json(value))（ptde_common 规范 JSON）。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Double,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from ptde_common.model_base import VersionedModel, canonical_json, sha256_hex

__all__ = ["FindingBase", "PtdeFindingModel", "FindingRepository", "ADOPT_STATUSES"]

ADOPT_STATUSES = ("pending", "adopted", "dismissed")


class FindingBase(DeclarativeBase):
    """ptde 专用声明基座（不依赖 server.db，保持 analyzer 无宿主依赖）。"""


class PtdeFindingModel(FindingBase):
    """ptde_finding 表 ORM（对齐 sql/init.sql §13.5 DDL）。"""

    __tablename__ = "ptde_finding"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    repo_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    finding_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    status_three: Mapped[str] = mapped_column(String(16), nullable=False)
    source_object: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    analyzer_version: Mapped[str] = mapped_column(String(32), nullable=False)
    adopt_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    legacy_hint_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    value_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    update_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )


class FindingRepository:
    """ptde_finding 仓储（engine 由调用方注入；支持 sqlite 测试）。"""

    def __init__(self, engine: Engine, *, create_all: bool = False) -> None:
        self._engine = engine
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        if create_all:
            FindingBase.metadata.create_all(engine)

    # ------------------------------------------------------------ 写路径

    def upsert_finding(
        self,
        *,
        repo_id: int | None,
        finding_type: str,
        value: dict | VersionedModel,
        status_three: str,
        source_object: str | None,
        evidence: list | None = None,
        confidence: float | None = None,
        analyzer_version: str,
        legacy_hint_id: int | None = None,
        adopt_status: str = "pending",
    ) -> int:
        """幂等 upsert：返回 finding id（命中更新不重复插行）。"""
        value_json = _dump_value(value)
        fingerprint = sha256_hex(canonical_json(value if isinstance(value, dict) else _dump_value(value)))
        evidence_json = _dump_evidence(evidence)
        with self._sessions() as s:
            existing = s.scalars(
                select(PtdeFindingModel)
                .where(PtdeFindingModel.repo_id == repo_id)
                .where(PtdeFindingModel.finding_type == finding_type)
                .where(PtdeFindingModel.source_object == source_object)
                .where(PtdeFindingModel.value_fingerprint == fingerprint)
                .limit(1)
            ).first()
            if existing is not None:
                existing.value_json = value_json
                existing.evidence_json = evidence_json
                existing.confidence = confidence
                existing.status_three = status_three
                existing.analyzer_version = analyzer_version
                s.commit()
                return existing.id
            row = PtdeFindingModel(
                repo_id=repo_id,
                finding_type=finding_type,
                value_json=value_json,
                status_three=status_three,
                source_object=source_object,
                evidence_json=evidence_json,
                confidence=confidence,
                analyzer_version=analyzer_version,
                adopt_status=adopt_status,
                legacy_hint_id=legacy_hint_id,
                value_fingerprint=fingerprint,
            )
            s.add(row)
            s.commit()
            return row.id

    def update_adopt_status(self, finding_id: int, adopt_status: str) -> bool:
        """采纳流转：pending → adopted / dismissed（非法状态显式拒绝）。"""
        if adopt_status not in ADOPT_STATUSES:
            raise ValueError(f"invalid adopt_status: {adopt_status}")
        with self._sessions() as s:
            row = s.get(PtdeFindingModel, finding_id)
            if row is None:
                return False
            row.adopt_status = adopt_status
            s.commit()
            return True

    # ------------------------------------------------------------ 读路径

    def by_id(self, finding_id: int) -> PtdeFindingModel | None:
        with self._sessions() as s:
            return s.get(PtdeFindingModel, finding_id)

    def list_by_type(self, finding_type: str, *, repo_id: int | None = None,
                     adopt_status: str | None = None, limit: int = 100) -> list[PtdeFindingModel]:
        with self._sessions() as s:
            q = select(PtdeFindingModel).where(PtdeFindingModel.finding_type == finding_type)
            if repo_id is not None:
                q = q.where(PtdeFindingModel.repo_id == repo_id)
            if adopt_status is not None:
                q = q.where(PtdeFindingModel.adopt_status == adopt_status)
            q = q.order_by(PtdeFindingModel.confidence.desc(), PtdeFindingModel.id.desc()).limit(limit)
            return list(s.scalars(q))

    def count_all(self) -> int:
        with self._sessions() as s:
            return int(s.scalar(select(func.count()).select_from(PtdeFindingModel)))


def _dump_value(value: dict | VersionedModel) -> str:
    if isinstance(value, VersionedModel):
        return value.to_json()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _dump_evidence(evidence: list | None) -> str | None:
    if not evidence:
        return None
    dumped = [
        e.model_dump(mode="json") if isinstance(e, VersionedModel) else e
        for e in evidence
    ]
    return json.dumps(dumped, ensure_ascii=False, default=str)
