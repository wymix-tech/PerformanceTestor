"""datagen 域数据层（阶段 5 · 步骤 4）：SQLAlchemy 对接 data_gen_task / semantic_hint / data_gen_plan。

替换 Java MyBatis 的 DataGenTaskMapper / SemanticHintMapper / DataGenPlanMapper（读路径）：
- tasks         → create_time DESC，limit 1..200 截断（DataGenService.list）
- suggestions   → repoId/tableName/hintType/status 可选过滤，confidence DESC, id DESC，
                  固定 limit 100（DataGenService.suggestions）
- plans         → repoId/status 可选过滤，create_time DESC, id DESC（DataGenPlanMapper.query）
序列化对齐 Java 实体 Jackson 形态（camelCase；null 字段透传 null）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Double, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from server.db import Base, BigIntPk


class DataGenTaskModel(Base):
    """data_gen_task：灌数据任务记录。"""

    __tablename__ = "data_gen_task"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    task_name: Mapped[str] = mapped_column(String(128), nullable=False)
    db_url: Mapped[str] = mapped_column(String(255), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    rule_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tables_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def to_camel(self) -> dict:
        return {
            "id": self.id,
            "taskName": self.task_name,
            "dbUrl": self.db_url,
            "tableName": self.table_name,
            "rowCount": self.row_count,
            "ruleJson": self.rule_json,
            "status": self.status,
            "inserted": self.inserted,
            "totalRows": self.total_rows,
            "tablesJson": self.tables_json,
            "detailJson": self.detail_json,
            "costMs": self.cost_ms,
            "errorMsg": self.error_msg,
            "createTime": self.create_time.isoformat(timespec="seconds"),
        }


class SemanticHintModel(Base):
    """semantic_hint：语义提示（魔法值 value / 表间关联 relation）。"""

    __tablename__ = "semantic_hint"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    repo_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hint_type: Mapped[str] = mapped_column(String(16), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    column_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    values_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_table: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    parent_column: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    source_refs: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def to_camel(self) -> dict:
        return {
            "id": self.id,
            "repoId": self.repo_id,
            "hintType": self.hint_type,
            "tableName": self.table_name,
            "columnName": self.column_name,
            "valuesJson": self.values_json,
            "parentTable": self.parent_table,
            "parentColumn": self.parent_column,
            "sourceRefs": self.source_refs,
            "source": self.source,
            "confidence": self.confidence,
            "status": self.status,
            "createTime": self.create_time.isoformat(timespec="seconds"),
        }


class DataGenPlanModel(Base):
    """data_gen_plan：接口驱动数据方案（分析源码推导，可回看/可复用）。"""

    __tablename__ = "data_gen_plan"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    repo_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    plan_name: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stress_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    conn_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    db_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tables_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def to_camel(self) -> dict:
        return {
            "id": self.id,
            "repoId": self.repo_id,
            "planName": self.plan_name,
            "endpointJson": self.endpoint_json,
            "stressJson": self.stress_json,
            "connId": self.conn_id,
            "dbUrl": self.db_url,
            "tablesJson": self.tables_json,
            "warningsJson": self.warnings_json,
            "status": self.status,
            "taskId": self.task_id,
            "createTime": self.create_time.isoformat(timespec="seconds"),
            "updateTime": self.update_time.isoformat(timespec="seconds"),
        }


class DatagenRepository:
    """datagen 读路径查询集合（语义对齐 Mapper XML；写路径暂由 Java 承担）。"""

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory

    def tasks(self, limit: int) -> list[DataGenTaskModel]:
        stmt = (select(DataGenTaskModel)
                .order_by(DataGenTaskModel.create_time.desc())
                .limit(max(1, min(limit, 200))))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def task_by_id(self, task_id: int) -> DataGenTaskModel | None:
        with self._sessions() as s:
            return s.get(DataGenTaskModel, task_id)

    def suggestions(self, repo_id: int | None, table_name: str | None,
                    hint_type: str | None, status: str | None,
                    limit: int = 100) -> list[SemanticHintModel]:
        stmt = (select(SemanticHintModel)
                .order_by(SemanticHintModel.confidence.desc(),
                          SemanticHintModel.id.desc())
                .limit(limit))
        if repo_id is not None:
            stmt = stmt.where(SemanticHintModel.repo_id == repo_id)
        if table_name:
            stmt = stmt.where(SemanticHintModel.table_name == table_name)
        if hint_type:
            stmt = stmt.where(SemanticHintModel.hint_type == hint_type)
        if status:
            stmt = stmt.where(SemanticHintModel.status == status)
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def plans(self, repo_id: int | None, status: str | None,
              limit: int) -> list[DataGenPlanModel]:
        stmt = (select(DataGenPlanModel)
                .order_by(DataGenPlanModel.create_time.desc(),
                          DataGenPlanModel.id.desc())
                .limit(max(1, min(limit, 200))))
        if repo_id is not None:
            stmt = stmt.where(DataGenPlanModel.repo_id == repo_id)
        if status:
            stmt = stmt.where(DataGenPlanModel.status == status)
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def plan_by_id(self, plan_id: int) -> DataGenPlanModel | None:
        with self._sessions() as s:
            return s.get(DataGenPlanModel, plan_id)
