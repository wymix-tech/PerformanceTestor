"""source 域数据层（阶段 5 · 步骤 5 读路径；步骤 9 写路径）：SQLAlchemy 对接 source_repo /
source_file / performance_issue / fix_suggestion。

读查询语义对齐 Mapper XML：
- repos          → create_time DESC，limit 1..100 截断（SourceManager.listRepos）
- files          → repo_id 过滤 + relative_path 前缀 LIKE，path 升序，limit 1..5000
- issues         → appName/issueType/status 过滤，create_time DESC，limit 1..200
- recent_slow_sqls → 近 7 天 + app 过滤，cost_ms DESC，limit 1..50（SourceIssueService）
- suggestions    → 按 issue_id 升序（findByIssue）

写方法语义对齐 SourceRepoMapper / SourceFileMapper：
- insert_repo      → status 固定 'importing'，file_count/total_size 置 0
- update_indexed   → status='ready' + file_count + total_size（导入/resync 收尾）
- update_status    → status + error_msg（导入失败/重同步失败）
- update_app_name / update_branch → 绑定应用（空串=解绑）/ 切换分支
- file 路径      → findByRepoAndPath 幂等去重、500 条一批 batchInsert、deleteByRepo 全量重建

source_repo 表模型在 collector_repo（项目准入共用），此处扩展其列集。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from server.collector_repo import SlowSqlModel, SourceRepoModel
from server.db import Base, BigIntPk

FILE_INSERT_CHUNK = 500


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
    # Java 侧由 DB DEFAULT CURRENT_TIMESTAMP 兜底（insert SQL 不含 create_time）
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                  default=datetime.now)

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
    """source 域读写集合（写路径步骤 9 收编，语义对齐 Java Mapper XML）。"""

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory

    # ------------------------------------------------------------ 写路径

    def insert_repo(self, repo: SourceRepoModel) -> int:
        """对齐 SourceRepoMapper.insert：status 固定 'importing'，计数置 0；返回生成主键。"""
        repo.status = "importing"
        repo.file_count = 0
        repo.total_size = 0
        with self._sessions() as s:
            s.add(repo)
            s.commit()
            s.refresh(repo)
            return repo.id

    def update_app_name(self, repo_id: int, app_name: str) -> None:
        with self._sessions() as s:
            repo = s.get(SourceRepoModel, repo_id)
            if repo is not None:
                repo.app_name = app_name
                s.commit()

    def update_status(self, repo_id: int, status: str, error_msg: str | None) -> None:
        with self._sessions() as s:
            repo = s.get(SourceRepoModel, repo_id)
            if repo is not None:
                repo.status = status
                repo.error_msg = error_msg
                s.commit()

    def update_indexed(self, repo_id: int, file_count: int, total_size: int) -> None:
        """对齐 updateIndexed：status='ready' + file_count + total_size。"""
        with self._sessions() as s:
            repo = s.get(SourceRepoModel, repo_id)
            if repo is not None:
                repo.status = "ready"
                repo.file_count = file_count
                repo.total_size = total_size
                s.commit()

    def update_branch(self, repo_id: int, branch: str) -> None:
        with self._sessions() as s:
            repo = s.get(SourceRepoModel, repo_id)
            if repo is not None:
                repo.branch = branch
                s.commit()

    def repo_by_id(self, repo_id: int) -> SourceRepoModel | None:
        """对齐 findById（DetailResultMap）：内部详情查询，含 git_token 供 git 操作使用。"""
        with self._sessions() as s:
            return s.get(SourceRepoModel, repo_id)

    def file_by_repo_and_path(self, repo_id: int, relative_path: str) -> SourceFileModel | None:
        stmt = (select(SourceFileModel)
                .where(SourceFileModel.repo_id == repo_id,
                       SourceFileModel.relative_path == relative_path)
                .limit(1))
        with self._sessions() as s:
            return s.scalars(stmt).first()

    def batch_insert_files(self, files: list[SourceFileModel]) -> int:
        """对齐 batchInsert（500 条一批）；create_time 由 DB 默认（Python 侧取当下）。"""
        now = datetime.now()
        for f in files:
            f.create_time = now
        with self._sessions() as s:
            for i in range(0, len(files), FILE_INSERT_CHUNK):
                s.add_all(files[i:i + FILE_INSERT_CHUNK])
                s.commit()
        return len(files)

    def delete_files_by_repo(self, repo_id: int) -> int:
        with self._sessions() as s:
            deleted = s.execute(
                sa_delete(SourceFileModel).where(SourceFileModel.repo_id == repo_id)).rowcount
            s.commit()
            return deleted

    def file_by_id(self, file_id: int) -> SourceFileModel | None:
        with self._sessions() as s:
            return s.get(SourceFileModel, file_id)

    def files_by_repo_contains(self, repo_id: int, keyword: str, limit: int) -> list[SourceFileModel]:
        """对齐 listByRepoContains：relative_path LIKE %keyword%，path 升序。"""
        stmt = (select(SourceFileModel)
                .where(SourceFileModel.repo_id == repo_id,
                       SourceFileModel.relative_path.like(f"%{keyword}%"))
                .order_by(SourceFileModel.relative_path.asc())
                .limit(max(1, limit)))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def files_by_name(self, repo_id: int, file_name: str, limit: int) -> list[SourceFileModel]:
        """对齐 listByName：file_name 精确匹配，path 升序。"""
        stmt = (select(SourceFileModel)
                .where(SourceFileModel.repo_id == repo_id,
                       SourceFileModel.file_name == file_name)
                .order_by(SourceFileModel.relative_path.asc())
                .limit(max(1, limit)))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    # ------------------------------------------------------ 问题与方案写路径

    def insert_issue(self, issue: PerformanceIssueModel) -> int:
        """对齐 PerformanceIssueMapper.insert：status 固定 'pending'；返回生成主键。"""
        issue.status = "pending"
        with self._sessions() as s:
            s.add(issue)
            s.commit()
            s.refresh(issue)
            return issue.id

    def update_issue_location(self, issue_id: int, source_repo_id: int,
                              source_file_id: int | None,
                              line_start: int | None, line_end: int | None) -> None:
        with self._sessions() as s:
            issue = s.get(PerformanceIssueModel, issue_id)
            if issue is not None:
                issue.source_repo_id = source_repo_id
                issue.source_file_id = source_file_id
                issue.line_start = line_start
                issue.line_end = line_end
                s.commit()

    def update_issue_stress_task(self, issue_id: int, stress_task_id: int) -> None:
        with self._sessions() as s:
            issue = s.get(PerformanceIssueModel, issue_id)
            if issue is not None:
                issue.stress_task_id = stress_task_id
                s.commit()

    def update_issue_analyzed(self, issue_id: int, root_cause: str) -> None:
        """对齐 updateAnalyzed：root_cause + status='analyzed'。"""
        with self._sessions() as s:
            issue = s.get(PerformanceIssueModel, issue_id)
            if issue is not None:
                issue.root_cause = root_cause
                issue.status = "analyzed"
                s.commit()

    def update_issue_evidence(self, issue_id: int, evidence_json: str) -> None:
        with self._sessions() as s:
            issue = s.get(PerformanceIssueModel, issue_id)
            if issue is not None:
                issue.evidence_json = evidence_json
                s.commit()

    def update_issue_verify(self, issue_id: int, verify_json: str,
                            verify_time, status: str | None) -> None:
        """对齐 IssueVerifyMapper.updateVerify：写 verify_json/verify_time，达标时置 resolved。"""
        with self._sessions() as s:
            issue = s.get(PerformanceIssueModel, issue_id)
            if issue is not None:
                issue.verify_json = verify_json
                issue.verify_time = verify_time
                if status is not None:
                    issue.status = status
                s.commit()

    def insert_suggestions(self, suggestions: list[FixSuggestionModel]) -> int:
        """对齐 batchInsert（一次性多值插入）。"""
        now = datetime.now()
        for fs in suggestions:
            fs.create_time = now
        with self._sessions() as s:
            s.add_all(suggestions)
            s.commit()
        return len(suggestions)

    def clear_selected_by_issue(self, issue_id: int) -> None:
        with self._sessions() as s:
            for fs in s.scalars(select(FixSuggestionModel)
                                .where(FixSuggestionModel.issue_id == issue_id)):
                fs.selected = 0
            s.commit()

    def update_selected(self, suggestion_id: int, selected: int) -> None:
        with self._sessions() as s:
            fs = s.get(FixSuggestionModel, suggestion_id)
            if fs is not None:
                fs.selected = selected
                s.commit()

    def update_prompt_md(self, suggestion_id: int, prompt_md: str) -> None:
        with self._sessions() as s:
            fs = s.get(FixSuggestionModel, suggestion_id)
            if fs is not None:
                fs.prompt_md = prompt_md
                s.commit()

    def slow_sql_by_id(self, slow_sql_id: int) -> SlowSqlModel | None:
        with self._sessions() as s:
            return s.get(SlowSqlModel, slow_sql_id)

    # ------------------------------------------------------------ 读路径

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
