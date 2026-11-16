"""stress 域数据层（阶段 5 · 步骤 2）：SQLAlchemy 2.0 对接 stress_task / stress_sample。

替换 Java MyBatis 的 StressTaskMapper / StressSampleMapper：
- 表名、列名与 sql/init_empty.sql DDL 完全一致（生产共用同一 MySQL 库；
  分区 DDL 由 SQL 脚本维护，ORM 不重建分区，仅做读写兼容）。
- 查询语义逐条对齐 Mapper XML：
  query          → 可选 status 过滤，ORDER BY create_time DESC，LIMIT（1..200 截断）
  findById       → 主键单查
  queryByScene   → appName + targetUrl + status 过滤，时间倒序（趋势图取数）
  queryByTask    → sample_time ASC，LIMIT（1..2000 截断）
- 序列化对齐 Java Jackson：camelCase 字段名、LocalDateTime → ISO 秒级字符串、
  errorMsg 为 null 时输出 null（契约测试 golden 的结构即由此而来）。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Double, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from server.db import Base

# sqlite 的 INTEGER PRIMARY KEY 才走 rowid 自增；MySQL 用 BIGINT（方言 variant 统一两侧）
BigIntPk = BigInteger().with_variant(Integer, "sqlite")


def _dt(value: datetime | None) -> str | None:
    """LocalDateTime → Jackson ISO 形态（秒级，如 2026-11-13T20:26:16）。"""
    return None if value is None else value.isoformat(timespec="seconds")


class StressTaskModel(Base):
    """stress_task：压测任务（列名对齐 sql/init_empty.sql）。"""

    __tablename__ = "stress_task"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    task_name: Mapped[str] = mapped_column(String(128), nullable=False)
    engine_type: Mapped[str] = mapped_column(String(32), nullable=False, default="http")
    target_url: Mapped[str] = mapped_column(String(512), nullable=False)
    app_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    app_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="GET")
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    qps_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finish_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_camel(self) -> dict:
        """对齐 Java StressTask 实体的 Jackson 序列化形态。"""
        return {
            "id": self.id,
            "taskName": self.task_name,
            "engineType": self.engine_type,
            "targetUrl": self.target_url,
            "method": self.method,
            "appName": self.app_name,
            "appVersion": self.app_version,
            "concurrency": self.concurrency,
            "durationSec": self.duration_sec,
            "qpsLimit": self.qps_limit,
            "status": self.status,
            "resultJson": self.result_json,
            "errorMsg": self.error_msg,
            "createTime": _dt(self.create_time),
            "finishTime": _dt(self.finish_time),
        }


class StressSampleModel(Base):
    """stress_sample：压测周期采样（按天分区，ORM 读写兼容）。"""

    __tablename__ = "stress_sample"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    sample_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qps: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    p50_ms: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    p95_ms: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    p99_ms: Mapped[float] = mapped_column(Double, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    def to_camel(self) -> dict:
        """对齐 Java StressSample 实体的 Jackson 序列化形态。"""
        return {
            "id": self.id,
            "taskId": self.task_id,
            "sampleTime": _dt(self.sample_time),
            "concurrency": self.concurrency,
            "qps": self.qps,
            "p50Ms": self.p50_ms,
            "p95Ms": self.p95_ms,
            "p99Ms": self.p99_ms,
            "errorCount": self.error_count,
        }


class StressRepository:
    """只读查询集合：语义对齐 MyBatis Mapper（阶段 5 仅迁移读路径，写路径后续跟进）。"""

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory

    def query(self, status: str | None, limit: int) -> list[StressTaskModel]:
        stmt = select(StressTaskModel).order_by(StressTaskModel.create_time.desc())
        if status:
            stmt = stmt.where(StressTaskModel.status == status)
        stmt = stmt.limit(max(1, min(limit, 200)))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def find_by_id(self, task_id: int) -> StressTaskModel | None:
        with self._sessions() as s:
            return s.get(StressTaskModel, task_id)

    def query_by_scene(self, app_name: str, target_url: str, status: str,
                       limit: int) -> list[StressTaskModel]:
        stmt = select(StressTaskModel).order_by(StressTaskModel.create_time.desc()).limit(limit)
        if status:
            stmt = stmt.where(StressTaskModel.status == status)
        if app_name:
            stmt = stmt.where(StressTaskModel.app_name == app_name)
        if target_url:
            stmt = stmt.where(StressTaskModel.target_url == target_url)
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def samples(self, task_id: int, limit: int) -> list[StressSampleModel]:
        stmt = (select(StressSampleModel)
                .where(StressSampleModel.task_id == task_id)
                .order_by(StressSampleModel.sample_time.asc())
                .limit(max(1, min(limit, 2000))))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def parse_result_json(self, task: StressTaskModel | None) -> dict | None:
        """resultJson → dict；空/坏 JSON 返回 None（对齐 Java parseResultJson 兜底）。"""
        if task is None or not task.result_json or not task.result_json.strip():
            return None
        try:
            return json.loads(task.result_json)
        except (ValueError, TypeError):
            return None
