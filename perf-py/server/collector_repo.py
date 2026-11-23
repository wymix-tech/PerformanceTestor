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

from sqlalchemy import (
    BigInteger,
    DateTime,
    Double,
    Integer,
    String,
    Text,
    and_,
    desc,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from server.db import Base

# sqlite 的 INTEGER PRIMARY KEY 才走 rowid 自增；MySQL 用 BIGINT（方言 variant 统一两侧）
BigIntPk = BigInteger().with_variant(Integer, "sqlite")

PROJECT_CACHE_TTL_S = 30.0
INSERT_CHUNK = 200
TRACE_INSERT_CHUNK = 50


def _fmt_dt(dt: datetime | None) -> str | None:
    """LocalDateTime → Jackson ISO 形态（秒级），与 stress_repo._dt 口径一致。"""
    return dt.isoformat(timespec="seconds") if dt is not None else None


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
    # P1-5 扩展指标（连接池/容器线程池/直接内存/Metaspace/线程状态细分）：
    # 旧探针不上报时为 NULL，独立扩展列保持旧表字段零破坏
    extra_metrics: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    # P1-1 压测流量染色：旧探针不上报时为 NULL（可选字段，向后兼容）
    stress_task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


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
    # P1-1 压测流量染色：旧探针不上报时为 NULL（可选字段，向后兼容）
    stress_task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


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
    # P1-1 压测流量染色：旧探针不上报时为 NULL（可选字段，向后兼容）
    stress_task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class HotspotMethodModel(Base):
    """hotspot_method：方法级 CPU 热点（agent 弱采样聚合上报）。"""

    __tablename__ = "hotspot_method"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    app_name: Mapped[str] = mapped_column(String(128), nullable=False)
    method_name: Mapped[str] = mapped_column(String(512), nullable=False)
    samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sample_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class JvmAlertRuleModel(Base):
    """jvm_alert_rule：JVM 阈值告警规则（app_name + metric_key 唯一）。"""

    __tablename__ = "jvm_alert_rule"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    app_name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_key: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    window_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    quiet_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    webhook: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_alert_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                  default=datetime.now)


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

    # ---------------------------------------------------------- 观测查询读路径

    def list_apps(self) -> list[dict]:
        """对齐 AppInstanceMapper.findAll：status DESC, last_report_time DESC。"""
        with self._sessions() as s:
            rows = list(s.scalars(
                select(AppInstanceModel)
                .order_by(desc(AppInstanceModel.status),
                          desc(AppInstanceModel.last_report_time))))
        return [{
            "id": r.id,
            "appName": r.app_name,
            "appHost": r.app_host,
            "appPort": r.app_port,
            "appVersion": r.app_version,
            "agentVersion": r.agent_version,
            "status": r.status,
            "lastReportTime": _fmt_dt(r.last_report_time),
            "createTime": _fmt_dt(r.create_time),
        } for r in rows]

    def _apply_cursor(self, stmt, time_col, id_col,
                      cursor_pair: tuple[datetime, int] | None):
        """游标条件：防同秒跳批必须 (time < ct) OR (time = ct AND id < cid)。"""
        if cursor_pair is None:
            return stmt
        ct, cid = cursor_pair
        return stmt.where(or_(time_col < ct, and_(time_col == ct, id_col < cid)))

    def query_jvm_metrics(
        self, app: str | None, start: datetime | None, end: datetime | None,
        cursor_pair: tuple[datetime, int] | None, limit_plus_one: int,
    ) -> list[JvmMetricModel]:
        stmt = select(JvmMetricModel)
        if app:
            stmt = stmt.where(JvmMetricModel.app_name == app)
        if start:
            stmt = stmt.where(JvmMetricModel.collect_time >= start)
        if end:
            stmt = stmt.where(JvmMetricModel.collect_time <= end)
        stmt = self._apply_cursor(stmt, JvmMetricModel.collect_time, JvmMetricModel.id,
                                  cursor_pair)
        stmt = (stmt.order_by(desc(JvmMetricModel.collect_time), desc(JvmMetricModel.id))
                .limit(limit_plus_one))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def query_traces(
        self, app: str | None, method: str | None, min_cost_ms: float | None,
        start: datetime | None, end: datetime | None,
        cursor_pair: tuple[datetime, int] | None, limit_plus_one: int,
        stress_task_id: int | None = None,
    ) -> list[TraceChainModel]:
        stmt = select(TraceChainModel)
        if app:
            stmt = stmt.where(TraceChainModel.app_name == app)
        if method:
            stmt = stmt.where(TraceChainModel.root_method.contains(method))
        if min_cost_ms is not None and min_cost_ms > 0:
            stmt = stmt.where(TraceChainModel.total_ms >= min_cost_ms)
        if stress_task_id is not None:
            stmt = stmt.where(TraceChainModel.stress_task_id == stress_task_id)
        if start:
            stmt = stmt.where(TraceChainModel.start_time >= start)
        if end:
            stmt = stmt.where(TraceChainModel.start_time <= end)
        stmt = self._apply_cursor(stmt, TraceChainModel.start_time, TraceChainModel.id,
                                  cursor_pair)
        stmt = (stmt.order_by(desc(TraceChainModel.start_time), desc(TraceChainModel.id))
                .limit(limit_plus_one))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def get_trace_by_id(self, trace_id: str) -> TraceChainModel | None:
        with self._sessions() as s:
            return s.scalars(
                select(TraceChainModel)
                .where(TraceChainModel.trace_id == trace_id)
                .limit(1)
            ).first()

    def query_slow_sqls(
        self, app: str | None, min_cost_ms: float | None,
        start: datetime | None, end: datetime | None,
        cursor_pair: tuple[datetime, int] | None, limit_plus_one: int,
        stress_task_id: int | None = None,
    ) -> list[SlowSqlModel]:
        stmt = select(SlowSqlModel)
        if app:
            stmt = stmt.where(SlowSqlModel.app_name == app)
        if min_cost_ms is not None and min_cost_ms > 0:
            stmt = stmt.where(SlowSqlModel.cost_ms >= min_cost_ms)
        if stress_task_id is not None:
            stmt = stmt.where(SlowSqlModel.stress_task_id == stress_task_id)
        if start:
            stmt = stmt.where(SlowSqlModel.execute_time >= start)
        if end:
            stmt = stmt.where(SlowSqlModel.execute_time <= end)
        stmt = self._apply_cursor(stmt, SlowSqlModel.execute_time, SlowSqlModel.id,
                                  cursor_pair)
        stmt = (stmt.order_by(desc(SlowSqlModel.execute_time), desc(SlowSqlModel.id))
                .limit(limit_plus_one))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def query_slow_commands(
        self, app: str | None, cmd_type: str | None, min_cost_ms: float | None,
        start: datetime | None, end: datetime | None,
        cursor_pair: tuple[datetime, int] | None, limit_plus_one: int,
        stress_task_id: int | None = None,
    ) -> list[SlowCommandModel]:
        stmt = select(SlowCommandModel)
        if app:
            stmt = stmt.where(SlowCommandModel.app_name == app)
        if cmd_type:
            stmt = stmt.where(SlowCommandModel.type == cmd_type.lower())
        if min_cost_ms is not None and min_cost_ms > 0:
            stmt = stmt.where(SlowCommandModel.cost_ms >= min_cost_ms)
        if stress_task_id is not None:
            stmt = stmt.where(SlowCommandModel.stress_task_id == stress_task_id)
        if start:
            stmt = stmt.where(SlowCommandModel.execute_time >= start)
        if end:
            stmt = stmt.where(SlowCommandModel.execute_time <= end)
        stmt = self._apply_cursor(stmt, SlowCommandModel.execute_time, SlowCommandModel.id,
                                  cursor_pair)
        stmt = (stmt.order_by(desc(SlowCommandModel.execute_time), desc(SlowCommandModel.id))
                .limit(limit_plus_one))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def query_hotspots(
        self, app: str | None, start: datetime, end: datetime, limit: int = 30,
    ) -> list[dict]:
        """对齐 HotspotMapper.aggregateByMethod：SUM(samples) DESC，limit ∈ [1, 200]。"""
        total_samples = func.sum(HotspotMethodModel.samples).label("total_samples")
        stmt = (
            select(HotspotMethodModel.method_name, total_samples)
            .where(HotspotMethodModel.sample_time >= start)
            .where(HotspotMethodModel.sample_time <= end)
        )
        if app:
            stmt = stmt.where(HotspotMethodModel.app_name == app)
        stmt = (
            stmt.group_by(HotspotMethodModel.method_name)
            .order_by(desc(total_samples))
            .limit(max(1, min(200, limit)))
        )
        with self._sessions() as s:
            return [{"methodName": m, "samples": int(cnt or 0)}
                    for m, cnt in s.execute(stmt).all()]

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
