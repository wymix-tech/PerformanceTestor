"""collector 域数据层（阶段 5 · 步骤 3）：SQLAlchemy 对接探针上报相关表。

覆盖 sql/init_empty.sql：app_instance / source_repo（项目准入）/ jvm_metric /
trace_chain / slow_sql / slow_command / hotspot_method / db_conn。
分区表（jvm_metric）的分区 DDL 由 SQL 脚本维护，ORM 不重建分区。

持久化语义对齐 MyBatis Mapper：
- upsert_app_instance：ON DUPLICATE KEY UPDATE——host/agent_version/心跳始终覆盖，
  本批未带端口（None）或版本（空串）时保留库中已有值；
- trace：trace_id 唯一键冲突时更新（重放幂等，ON DUPLICATE KEY UPDATE 等价实现）；
- 其余批量插入按 200 条分片（trace 按 50 条）提交，防 max_allowed_packet 超限。
"""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Double, Integer, String, Text, or_, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from server.db import Base

# sqlite 的 INTEGER PRIMARY KEY 才走 rowid 自增；MySQL 用 BIGINT（方言 variant 统一两侧）
BigIntPk = BigInteger().with_variant(Integer, "sqlite")

PROJECT_CACHE_TTL_S = 30.0
INSERT_CHUNK = 200
TRACE_INSERT_CHUNK = 50


class AppInstanceModel(Base):
    """app_instance：被测应用实例（agent 心跳注册，app_name 唯一）。"""

    __tablename__ = "app_instance"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    app_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    app_host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    app_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    app_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_report_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                  default=datetime.now)


class SourceRepoModel(Base):
    """source_repo：源码仓库（项目准入 + source 域仓库列表共用）。"""

    __tablename__ = "source_repo"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    repo_name: Mapped[str] = mapped_column(String(128), nullable=False)
    repo_type: Mapped[str] = mapped_column(String(16), nullable=False, default="zip")
    source_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    branch: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    app_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    git_user: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    git_token: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="importing")
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                  default=datetime.now)

    def to_camel(self) -> dict:
        """对齐 Java SourceRepo 实体 Jackson 形态；gitToken 为 @JsonIgnore 永不返回。"""
        return {
            "id": self.id,
            "repoName": self.repo_name,
            "repoType": self.repo_type,
            "sourcePath": self.source_path,
            "branch": self.branch,
            "appName": self.app_name,
            "gitUser": self.git_user,
            "status": self.status,
            "fileCount": self.file_count,
            "totalSize": self.total_size,
            "errorMsg": self.error_msg,
            "createTime": self.create_time.isoformat(timespec="seconds"),
        }


class JvmMetricModel(Base):
    """jvm_metric：JVM 指标时序（按天分区，读写兼容）。"""

    __tablename__ = "jvm_metric"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    app_name: Mapped[str] = mapped_column(String(128), nullable=False)
    collect_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    heap_used_mb: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    heap_max_mb: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    non_heap_used_mb: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    young_gc_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    young_gc_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    full_gc_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    full_gc_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    thread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cpu_usage: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    system_cpu: Mapped[float] = mapped_column(Double, nullable=False, default=0)


class TraceChainModel(Base):
    """trace_chain：业务方法调用链（trace_id 唯一，重放幂等更新）。"""

    __tablename__ = "trace_chain"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    app_name: Mapped[str] = mapped_column(String(128), nullable=False)
    root_method: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    trace_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    total_ms: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SlowSqlModel(Base):
    """slow_sql：慢 SQL 记录（jdbc 拦截 + 慢日志解析双通道）。"""

    __tablename__ = "slow_sql"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    app_name: Mapped[str] = mapped_column(String(128), nullable=False)
    sql_text: Mapped[str] = mapped_column(Text, nullable=False)
    cost_ms: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="jdbc")
    db_host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    method_chain: Mapped[str | None] = mapped_column(Text, nullable=True)
    execute_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SlowCommandModel(Base):
    """slow_command：中间件慢命令（Redis / ES / MQ）。"""

    __tablename__ = "slow_command"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    app_name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    command: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    cost_ms: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    method_chain: Mapped[str | None] = mapped_column(Text, nullable=True)
    execute_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class HotspotMethodModel(Base):
    """hotspot_method：方法级 CPU 热点（agent 弱采样聚合上报）。"""

    __tablename__ = "hotspot_method"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    app_name: Mapped[str] = mapped_column(String(128), nullable=False)
    method_name: Mapped[str] = mapped_column(String(512), nullable=False)
    samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sample_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DbConnModel(Base):
    """db_conn：数据库连接配置（慢 SQL 携带库地址时自动注册）。"""

    __tablename__ = "db_conn"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    conn_name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    app_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    db_url: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    password: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    is_default: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                  default=datetime.now)


class CollectorRepository:
    """探针上报持久化集合（语义对齐 Java collector 域 Mapper）。"""

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory
        self._project_cache: dict[str, tuple[bool, float]] = {}

    # ---------------------------------------------------------- 项目准入

    def project_exists(self, project_name: str | None) -> bool:
        """项目是否存在（30s 缓存，对齐 ProjectRegistry）。"""
        if not project_name or not project_name.strip():
            return False
        now = time.monotonic()
        cached = self._project_cache.get(project_name)
        if cached is not None and now - cached[1] < PROJECT_CACHE_TTL_S:
            return cached[0]
        with self._sessions() as s:
            exists = s.scalars(select(SourceRepoModel.id)
                               .where(SourceRepoModel.repo_name == project_name)
                               .limit(1)).first() is not None
        self._project_cache[project_name] = (exists, now)
        return exists

    def evict_project(self, project_name: str | None) -> None:
        """项目新建/改名/删除后失效缓存（对齐 ProjectRegistry.evict）。"""
        if project_name:
            self._project_cache.pop(project_name, None)

    # ---------------------------------------------------------- 心跳注册

    def upsert_app_instance(self, app_name: str, app_host: str, app_port: int | None,
                            app_version: str, agent_version: str,
                            last_report_time: datetime) -> None:
        """对齐 AppInstanceMapper.upsertByAppName（ON DUPLICATE KEY UPDATE 语义）。"""
        with self._sessions() as s:
            inst = s.scalars(select(AppInstanceModel)
                             .where(AppInstanceModel.app_name == app_name)
                             .limit(1)).first()
            if inst is None:
                s.add(AppInstanceModel(app_name=app_name, app_host=app_host,
                                       app_port=app_port, app_version=app_version,
                                       agent_version=agent_version, status=1,
                                       last_report_time=last_report_time))
            else:
                inst.app_host = app_host
                # 本批未带端口/版本时不覆盖已有值（对齐 IF(VALUES(...) IS NULL / ='') 分支）
                if app_port is not None:
                    inst.app_port = app_port
                if app_version != "":
                    inst.app_version = app_version
                inst.agent_version = agent_version
                inst.status = 1
                inst.last_report_time = last_report_time
            s.commit()

    # ---------------------------------------------------------- 批量落库

    def save_traces(self, rows: list[TraceChainModel]) -> int:
        """trace：trace_id 冲突时更新（重放幂等）；按 50 条分片提交。"""
        saved = 0
        with self._sessions() as s:
            for row in rows:
                existing = s.scalars(select(TraceChainModel)
                                     .where(TraceChainModel.trace_id == row.trace_id)
                                     .limit(1)).first()
                if existing is None:
                    s.add(row)
                else:
                    existing.root_method = row.root_method
                    existing.trace_json = row.trace_json
                    existing.total_ms = row.total_ms
                    existing.start_time = row.start_time
                saved += 1
                if saved % TRACE_INSERT_CHUNK == 0:
                    s.commit()
            s.commit()
        return saved

    def _save_chunked(self, rows, chunk: int = INSERT_CHUNK) -> int:
        with self._sessions() as s:
            for i in range(0, len(rows), max(1, chunk)):
                s.add_all(rows[i:i + max(1, chunk)])
                s.commit()
        return len(rows)

    def save_jvm_metrics(self, rows: list[JvmMetricModel]) -> int:
        return self._save_chunked(rows)

    def save_slow_sqls(self, rows: list[SlowSqlModel]) -> int:
        return self._save_chunked(rows)

    def save_slow_commands(self, rows: list[SlowCommandModel]) -> int:
        return self._save_chunked(rows)

    def save_hotspots(self, rows: list[HotspotMethodModel]) -> int:
        # Java 侧 hotspot 一次性批量插入，无分片
        with self._sessions() as s:
            s.add_all(rows)
            s.commit()
        return len(rows)

    # ---------------------------------------------------------- db_conn 自动注册

    def find_conns_for(self, app_name: str, db_host: str) -> list[DbConnModel]:
        """候选连接：app_name 绑定或 host 匹配（含双向包含，对齐 findByAppOrHost）。

        db_conn 为用户手工维护的小表，取全量后按 Java 匹配逻辑在 Python 侧过滤，
        保证 host 双向 LIKE 语义跨方言（MySQL CONCAT / SQLite）一致。
        """
        with self._sessions() as s:
            rows = list(s.scalars(
                select(DbConnModel)
                .where(or_(DbConnModel.app_name == app_name, DbConnModel.host != ""))
                .order_by(DbConnModel.is_default.desc(), DbConnModel.id.asc())
                .limit(20)))
        matched = []
        for c in rows:
            app_bound = c.app_name == app_name
            host_match = bool(c.host) and (db_host in c.host or c.host in db_host)
            if app_bound or host_match:
                matched.append(c)
        return matched

    def insert_db_conn(self, conn: DbConnModel) -> None:
        with self._sessions() as s:
            s.add(conn)
            s.commit()

    def match_order(self) -> list[DbConnModel]:
        """对齐 DbConnMapper.matchOrder：is_default DESC, id ASC，LIMIT 10（含凭据列）。"""
        stmt = (select(DbConnModel)
                .order_by(DbConnModel.is_default.desc(), DbConnModel.id.asc())
                .limit(10))
        with self._sessions() as s:
            return list(s.scalars(stmt))
