"""datagen 域数据层（阶段 5 · 步骤 4 读路径；步骤 7 写路径）：SQLAlchemy 对接 data_gen_task / semantic_hint / data_gen_plan。

替换 Java MyBatis 的 DataGenTaskMapper / SemanticHintMapper / DataGenPlanMapper：
- tasks         → create_time DESC，limit 1..200 截断（DataGenService.list）
- suggestions   → repoId/tableName/hintType/status 可选过滤，confidence DESC, id DESC，
                  固定 limit 100（DataGenService.suggestions）
- plans         → repoId/status 可选过滤，create_time DESC, id DESC（DataGenPlanMapper.query）
- 写路径：insert（total_rows=COALESCE(totalRows,rowCount)、status='pending'、inserted=0）、
  updateStatus/updateProgress/updateDetail/updateFinished、hint upsert
  （uk_hint 唯一键 ON DUPLICATE KEY UPDATE 语义）、hint updateStatus
序列化对齐 Java 实体 Jackson 形态（camelCase；null 字段透传 null）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Double, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from server.collector_repo import AppInstanceModel
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
    """datagen 读写集合（查询与写入语义逐条对齐 Mapper XML）。"""

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory

    # ------------------------------------------------------------ 写路径

    def insert_task(self, *, task_name: str, db_url: str, table_name: str,
                    row_count: int, total_rows: int, tables_json: str | None,
                    rule_json: str | None) -> int:
        """对齐 DataGenTaskMapper.insert：status='pending'、inserted=0、
        total_rows=COALESCE(totalRows, rowCount)。create_time 依赖 DB 默认值
        （wire 返回 null，与 Java 插入后实体未回填一致）。"""
        row = DataGenTaskModel(
            task_name=task_name, db_url=db_url or "", table_name=table_name or "",
            row_count=row_count, total_rows=total_rows if total_rows is not None else row_count,
            tables_json=tables_json, rule_json=rule_json,
            status="pending", inserted=0, create_time=datetime.now(),
        )
        with self._sessions() as s:
            s.add(row)
            s.commit()
            return row.id

    def update_task_status(self, task_id: int, status: str) -> None:
        with self._sessions() as s:
            row = s.get(DataGenTaskModel, task_id)
            if row is not None:
                row.status = status
                s.commit()

    def update_progress(self, task_id: int, inserted: int) -> None:
        with self._sessions() as s:
            row = s.get(DataGenTaskModel, task_id)
            if row is not None:
                row.inserted = inserted
                s.commit()

    def update_detail(self, task_id: int, detail_json: str, total_rows: int) -> None:
        with self._sessions() as s:
            row = s.get(DataGenTaskModel, task_id)
            if row is not None:
                row.detail_json = detail_json
                row.total_rows = total_rows
                s.commit()

    def update_task_finished(self, task_id: int, status: str, inserted: int,
                             cost_ms: int, error_msg: str | None) -> None:
        with self._sessions() as s:
            row = s.get(DataGenTaskModel, task_id)
            if row is not None:
                row.status = status
                row.inserted = inserted
                row.cost_ms = cost_ms
                row.error_msg = error_msg
                s.commit()

    def hint_upsert(self, *, repo_id: int | None, hint_type: str, table_name: str,
                    column_name: str | None, values_json: str | None,
                    parent_table: str, parent_column: str, source_refs: str | None,
                    source: str, confidence: float | None,
                    status: str = "pending") -> None:
        """对齐 SemanticHintMapper.upsert：uk_hint(repo_id,hint_type,table_name,
        column_name,parent_table) 命中时更新 values_json/parent_column/source_refs/
        source/confidence（ON DUPLICATE KEY UPDATE 语义；status 保留原值）。"""
        with self._sessions() as s:
            existing = s.scalars(select(SemanticHintModel)
                                 .where(SemanticHintModel.repo_id == repo_id)
                                 .where(SemanticHintModel.hint_type == hint_type)
                                 .where(SemanticHintModel.table_name == table_name)
                                 .where(SemanticHintModel.column_name == column_name)
                                 .where(SemanticHintModel.parent_table == parent_table)
                                 .limit(1)).first()
            if existing is not None:
                existing.values_json = values_json
                existing.parent_column = parent_column
                existing.source_refs = source_refs
                existing.source = source
                existing.confidence = confidence
            else:
                s.add(SemanticHintModel(
                    repo_id=repo_id, hint_type=hint_type, table_name=table_name,
                    column_name=column_name, values_json=values_json,
                    parent_table=parent_table, parent_column=parent_column,
                    source_refs=source_refs, source=source,
                    confidence=confidence, status=status,
                    create_time=datetime.now()))
            s.commit()

    def hint_by_id(self, hint_id: int) -> SemanticHintModel | None:
        with self._sessions() as s:
            return s.get(SemanticHintModel, hint_id)

    def hint_update_status(self, hint_id: int, status: str) -> None:
        with self._sessions() as s:
            row = s.get(SemanticHintModel, hint_id)
            if row is not None:
                row.status = status
                s.commit()

    # ------------------------------------------------------------ 跨域读取

    def find_repo(self, repo_id: int):
        """source_repo 单查（SourceFileProvider.loadFiles / selfCheck 用）。"""
        from server.collector_repo import SourceRepoModel

        with self._sessions() as s:
            return s.get(SourceRepoModel, repo_id)

    def list_indexed_files(self, repo_id: int, limit: int = 200) -> list:
        """source_file 索引（relative_path 升序；SourceFileMapper.listByRepo 语义）。"""
        from server.collector_repo import SourceFileModel

        stmt = (select(SourceFileModel)
                .where(SourceFileModel.repo_id == repo_id)
                .order_by(SourceFileModel.relative_path.asc())
                .limit(max(1, min(limit, 5000))))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def find_app_instance(self, app_name: str | None):
        """app_instance 单查（selfCheck 用；AppInstanceMapper.findByAppName）。"""
        if not app_name:
            return None
        with self._sessions() as s:
            return s.scalars(select(AppInstanceModel)
                             .where(AppInstanceModel.app_name == app_name)
                             .limit(1)).first()

    def find_db_conn(self, conn_id: int):
        """db_conn 单查（connId → 连接三元组；DbConnMapper.findById）。"""
        from server.collector_repo import DbConnModel

        with self._sessions() as s:
            return s.get(DbConnModel, conn_id)

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
