"""stress 域数据层（阶段 5 · 步骤 2 读路径；写路径步骤 6）：SQLAlchemy 2.0 对接 stress_task / stress_sample。

替换 Java MyBatis 的 StressTaskMapper / StressSampleMapper：
- 表名、列名与 sql/init_empty.sql DDL 完全一致（生产共用同一 MySQL 库；
  分区 DDL 由 SQL 脚本维护，ORM 不重建分区，仅做读写兼容）。
- 查询语义逐条对齐 Mapper XML：
  query          → 可选 status 过滤，ORDER BY create_time DESC，LIMIT（1..200 截断）
  findById       → 主键单查
  queryByScene   → appName + targetUrl + status 过滤，时间倒序（趋势图取数）
  queryByTask    → sample_time ASC，LIMIT（1..2000 截断）
  insert / updateStatus / updateFinished / updateFailed / batchInsert（写路径）
- 序列化对齐 Java Jackson：camelCase 字段名、LocalDateTime → ISO 秒级字符串、
  errorMsg 为 null 时输出 null（契约测试 golden 的结构即由此而来）。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Double, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from server.db import Base, BigIntPk


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
    # P0-4 修复验证重放：创建时持久化完整压测请求快照（body/headers/scenario/
    # warmupSec/stages/dataset 等），重放 100% 同构（列经 init.sql 幂等 ALTER 补齐）
    request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    # T7.3 数据集绑定（实施方案 §12.6 加列；旧库经 init.sql 幂等 ALTER 补齐）
    dataset_instance_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dataset_definition_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dataset_schema_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dataset_validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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
            # requestJson 仅库内持久化（P0-4 重放快照）：Java 实体无此字段，
            # 不进 wire 契约（tests/contract golden 键集合对齐绞杀者纪律）
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
    """读写集合：查询与写入语义逐条对齐 MyBatis Mapper。"""

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory

    # ------------------------------------------------------------ 写路径

    def insert_task(self, *, task_name: str, engine_type: str | None, target_url: str,
                    app_name: str, app_version: str, method: str | None,
                    concurrency: int, duration_sec: int, qps_limit: int,
                    request_json: str | None = None,
                    dataset_instance_id: int | None = None,
                    dataset_definition_version: str | None = None,
                    dataset_schema_fingerprint: str | None = None,
                    dataset_validation_json: str | None = None) -> int:
        """对齐 StressTaskMapper.insert：状态固定 pending，返回自增主键。

        T7.3：dataset_* 绑定列为可选快照（未绑定缺省 None，wire 契约不变）。
        注意 create_time 依赖 DB 默认值（MySQL DEFAULT CURRENT_TIMESTAMP），
        与 Java 同款——插入后立刻返回的实体不含 create_time（wire 契约 createTime=null）。
        """
        row = StressTaskModel(
            task_name=task_name, engine_type=engine_type or "http", target_url=target_url,
            app_name=app_name, app_version=app_version, method=method or "GET",
            concurrency=concurrency, duration_sec=duration_sec, qps_limit=qps_limit,
            request_json=request_json,
            dataset_instance_id=dataset_instance_id,
            dataset_definition_version=dataset_definition_version,
            dataset_schema_fingerprint=dataset_schema_fingerprint,
            dataset_validation_json=dataset_validation_json,
            status="pending", create_time=datetime.now(),
        )
        with self._sessions() as s:
            s.add(row)
            s.commit()
            return row.id

    def dataset_binding(self, task_id: int) -> dict | None:
        """T7.3 反查：task → dataset_* 绑定快照（未绑定返回 None）。"""
        row = self.find_by_id(task_id)
        if row is None or row.dataset_instance_id is None:
            return None
        return {
            "taskId": row.id,
            "datasetInstanceId": str(row.dataset_instance_id),
            "datasetDefinitionVersion": row.dataset_definition_version,
            "datasetSchemaFingerprint": row.dataset_schema_fingerprint,
            "datasetValidation": (
                json.loads(row.dataset_validation_json)
                if row.dataset_validation_json else None),
        }

    def update_status(self, task_id: int, status: str) -> None:
        with self._sessions() as s:
            row = s.get(StressTaskModel, task_id)
            if row is not None:
                row.status = status
                s.commit()

    def update_result_json(self, task_id: int, result_json: str) -> None:
        """仅更新 result_json 列（容量评估回写用），不改动 status/finish_time。"""
        with self._sessions() as s:
            row = s.get(StressTaskModel, task_id)
            if row is not None:
                row.result_json = result_json
                s.commit()

    def update_finished(self, task_id: int, result_json: str,
                        finish_time: datetime) -> None:
        with self._sessions() as s:
            row = s.get(StressTaskModel, task_id)
            if row is not None:
                row.status = "finished"
                row.result_json = result_json
                row.finish_time = finish_time
                s.commit()

    def update_failed(self, task_id: int, error_msg: str | None) -> None:
        """对齐 updateFailed：error_msg + finish_time = NOW()。"""
        with self._sessions() as s:
            row = s.get(StressTaskModel, task_id)
            if row is not None:
                row.status = "failed"
                row.error_msg = error_msg
                row.finish_time = datetime.now()
                s.commit()

    def insert_sample(self, *, task_id: int, sample_time: datetime, concurrency: int,
                      qps: float, p50_ms: float, p95_ms: float, p99_ms: float,
                      error_count: int) -> None:
        """对齐 StressSampleMapper.batchInsert（分区表主键含 sample_time，须显式写入）。"""
        with self._sessions() as s:
            s.add(StressSampleModel(
                task_id=task_id, sample_time=sample_time, concurrency=concurrency,
                qps=qps, p50_ms=p50_ms, p95_ms=p95_ms, p99_ms=p99_ms,
                error_count=error_count))
            s.commit()

    def find_app_version(self, app_name: str | None) -> str:
        """对齐 StressTaskService.resolveCurrentVersion：app_instance 心跳维护的当前版本。"""
        if not app_name or not app_name.strip():
            return ""
        from server.collector_repo import AppInstanceModel

        try:
            with self._sessions() as s:
                inst = s.scalars(select(AppInstanceModel)
                                 .where(AppInstanceModel.app_name == app_name)
                                 .limit(1)).first()
                return inst.app_version if inst is not None and inst.app_version else ""
        except Exception:  # noqa: BLE001 — 对齐 Java：查询失败返回空串不阻断建任务
            return ""

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

    def query_by_task(self, task_id: int, limit: int) -> list[StressSampleModel]:
        """对齐 StressSampleMapper.queryByTask：采样点时间升序（RT 基线取数）。"""
        stmt = (select(StressSampleModel)
                .where(StressSampleModel.task_id == task_id)
                .order_by(StressSampleModel.sample_time.asc())
                .limit(max(1, min(limit, 2000))))
        with self._sessions() as s:
            return list(s.scalars(stmt))

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
