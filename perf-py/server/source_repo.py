"""source 域数据层（阶段 5 · 步骤 5）：SQLAlchemy 对接 source_file / performance_issue / fix_suggestion。

读查询语义对齐 Mapper XML：
- repos          → create_time DESC，limit 1..100 截断（SourceManager.listRepos）
- files          → repo_id 过滤 + relative_path 前缀 LIKE，path 升序，limit 1..5000
- issues         → appName/issueType/status 过滤，create_time DESC，limit 1..200
- recent_slow_sqls → 近 7 天 + app 过滤，cost_ms DESC，limit 1..50（SourceIssueService）
- suggestions    → 按 issue_id 升序（findByIssue）

source_repo 表模型在 collector_repo（项目准入共用），此处扩展其列集。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from server.collector_repo import SlowSqlModel, SourceRepoModel
from server.db import Base, BigIntPk


class SourceFileModel(Base):
    """source_file：源码文件索引（内容存磁盘，表内仅元数据）。"""

    __tablename__ = "source_file"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_name: Mapped[str] = mapped_column(String(256), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def to_camel(self) -> dict:
        return {
            "id": self.id,
            "repoId": self.repo_id,
            "relativePath": self.relative_path,
            "fileName": self.file_name,
            "language": self.language,
            "size": self.size,
            "contentHash": self.content_hash,
            "createTime": self.create_time.isoformat(timespec="seconds"),
        }


class PerformanceIssueModel(Base):
    """performance_issue：性能问题（关联检测证据与源码位置）。"""

    __tablename__ = "performance_issue"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    issue_type: Mapped[str] = mapped_column(String(32), nullable=False)
    app_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_repo_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_file_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    stress_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    verify_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    verify_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def to_camel(self) -> dict:
        return {
            "id": self.id,
            "issueType": self.issue_type,
            "appName": self.app_name,
            "title": self.title,
            "evidenceJson": self.evidence_json,
            "sourceRepoId": self.source_repo_id,
            "sourceFileId": self.source_file_id,
            "lineStart": self.line_start,
            "lineEnd": self.line_end,
            "rootCause": self.root_cause,
            "status": self.status,
            "stressTaskId": self.stress_task_id,
            "verifyJson": self.verify_json,
            "verifyTime": self.verify_time.isoformat(timespec="seconds")
                          if self.verify_time else None,
            "createTime": self.create_time.isoformat(timespec="seconds"),
        }


class FixSuggestionModel(Base):
    """fix_suggestion：修复方案（每个问题 2-3 条，选定后生成修复提示词）。"""

    __tablename__ = "fix_suggestion"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    files_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    changes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_changes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    benefit: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def to_camel(self) -> dict:
        return {
            "id": self.id,
            "issueId": self.issue_id,
            "title": self.title,
            "filesJson": self.files_json,
            "changesJson": self.changes_json,
            "codeChangesJson": self.code_changes_json,
            "benefit": self.benefit,
            "risk": self.risk,
            "promptMd": self.prompt_md,
            "selected": self.selected,
            "createTime": self.create_time.isoformat(timespec="seconds"),
        }


class SourceRepository:
    """source 域读路径查询集合（写路径与磁盘操作暂由 Java 承担）。"""

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory

    def repos(self, limit: int) -> list[SourceRepoModel]:
        stmt = (select(SourceRepoModel)
                .order_by(SourceRepoModel.create_time.desc())
                .limit(max(1, min(limit, 100))))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def files(self, repo_id: int, prefix: str | None, limit: int) -> list[SourceFileModel]:
        stmt = (select(SourceFileModel)
                .where(SourceFileModel.repo_id == repo_id)
                .order_by(SourceFileModel.relative_path.asc())
                .limit(max(1, min(limit, 5000))))
        if prefix:
            stmt = stmt.where(SourceFileModel.relative_path.like(f"{prefix}%"))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def issues(self, app_name: str | None, issue_type: str | None,
               status: str | None, limit: int) -> list[PerformanceIssueModel]:
        stmt = (select(PerformanceIssueModel)
                .order_by(PerformanceIssueModel.create_time.desc())
                .limit(max(1, min(limit, 200))))
        if app_name:
            stmt = stmt.where(PerformanceIssueModel.app_name == app_name)
        if issue_type:
            stmt = stmt.where(PerformanceIssueModel.issue_type == issue_type)
        if status:
            stmt = stmt.where(PerformanceIssueModel.status == status)
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def issue_by_id(self, issue_id: int) -> PerformanceIssueModel | None:
        with self._sessions() as s:
            return s.get(PerformanceIssueModel, issue_id)

    def suggestions_by_issue(self, issue_id: int) -> list[FixSuggestionModel]:
        stmt = (select(FixSuggestionModel)
                .where(FixSuggestionModel.issue_id == issue_id)
                .order_by(FixSuggestionModel.id.asc()))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def suggestion_by_id(self, suggestion_id: int) -> FixSuggestionModel | None:
        with self._sessions() as s:
            return s.get(FixSuggestionModel, suggestion_id)

    def recent_slow_sqls(self, app: str | None, limit: int) -> list[SlowSqlModel]:
        """最近 7 天慢 SQL（cost_ms 倒序，1..50 截断，对齐 SourceIssueService）。"""
        now = datetime.now()
        stmt = (select(SlowSqlModel)
                .where(SlowSqlModel.execute_time >= now - timedelta(days=7))
                .where(SlowSqlModel.execute_time <= now)
                .order_by(SlowSqlModel.cost_ms.desc())
                .limit(max(1, min(limit, 50))))
        if app:
            stmt = stmt.where(SlowSqlModel.app_name == app)
        with self._sessions() as s:
            return list(s.scalars(stmt))


def slow_sql_to_camel(row: SlowSqlModel) -> dict:
    """对齐 Java SlowSql 实体 Jackson 形态（8 字段）。"""
    return {
        "id": row.id,
        "appName": row.app_name,
        "sqlText": row.sql_text,
        "costMs": row.cost_ms,
        "source": row.source,
        "dbHost": row.db_host,
        "methodChain": row.method_chain,
        "executeTime": row.execute_time.isoformat(timespec="seconds"),
    }
