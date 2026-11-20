"""定时压测调度域：stress_schedule 模型 / 仓储 / 触发服务（P0-1b）。

表结构对齐 sql/init_empty.sql §4.2 stress_schedule；wire 形态对齐
perf-web SchedulePanel.vue（scheduleName/intervalMinutes/enabled/lastRunTime/
nextRunTime/lastTaskId）。触发语义：enabled 且 next_run_time 到期 → 用
task_json 模板走 StressTaskService.create_and_run（与手工发起同一条链路，
含校验与数据集绑定解析）→ 更新 last_task_id / last_run_time / next_run_time。

调度循环为常驻守护线程（app.py 启动）；run_due() 为公共触发缝供测试与
手动补偿（线程只做扫描节拍，判定逻辑全部在 run_due 内，可单测）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from server.db import Base, BigIntPk
from server.notify import send as notify_send

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_MIN = 60


class StressScheduleModel(Base):
    """stress_schedule：定时压测调度（task_json 为压测任务模板）。"""

    __tablename__ = "stress_schedule"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    schedule_name: Mapped[str] = mapped_column(String(128), nullable=False)
    task_json: Mapped[str] = mapped_column(Text, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False,
                                                  default=DEFAULT_INTERVAL_MIN)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    baseline_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    webhook_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    last_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                  default=datetime.now)

    def to_camel(self) -> dict:
        return {
            "id": self.id,
            "scheduleName": self.schedule_name,
            "intervalMinutes": self.interval_minutes,
            "enabled": self.enabled,
            "baselineTaskId": self.baseline_task_id,
            "webhookUrl": self.webhook_url,
            "lastTaskId": self.last_task_id,
            "lastRunTime": _dt(self.last_run_time),
            "nextRunTime": _dt(self.next_run_time),
            "createTime": _dt(self.create_time),
        }


def _dt(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


class StressScheduleRepository:
    """stress_schedule 读写集合。"""

    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory

    def list(self, limit: int = 100) -> list[StressScheduleModel]:
        stmt = (select(StressScheduleModel)
                .order_by(StressScheduleModel.id.desc())
                .limit(max(1, min(limit, 200))))
        with self._sessions() as s:
            return list(s.scalars(stmt))

    def find_by_id(self, schedule_id: int) -> StressScheduleModel | None:
        with self._sessions() as s:
            return s.get(StressScheduleModel, schedule_id)

    def insert(self, *, schedule_name: str, task_json: str,
               interval_minutes: int, baseline_task_id: int = 0,
               webhook_url: str = "", enabled: bool = True,
               next_run_time: datetime | None = None) -> StressScheduleModel:
        row = StressScheduleModel(
            schedule_name=schedule_name, task_json=task_json,
            interval_minutes=max(1, interval_minutes),
            baseline_task_id=max(0, baseline_task_id),
            webhook_url=webhook_url or "",
            enabled=1 if enabled else 0,
            next_run_time=next_run_time, create_time=datetime.now())
        with self._sessions() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
        return row

    def update_trigger(self, schedule_id: int, *, last_task_id: int,
                       last_run_time: datetime, next_run_time: datetime) -> None:
        with self._sessions() as s:
            row = s.get(StressScheduleModel, schedule_id)
            if row is not None:
                row.last_task_id = last_task_id
                row.last_run_time = last_run_time
                row.next_run_time = next_run_time
                s.commit()

    def set_enabled(self, schedule_id: int, enabled: bool) -> StressScheduleModel | None:
        with self._sessions() as s:
            row = s.get(StressScheduleModel, schedule_id)
            if row is None:
                return None
            row.enabled = 1 if enabled else 0
            if enabled and row.next_run_time is None:
                row.next_run_time = datetime.now() \
                    + timedelta(minutes=max(1, row.interval_minutes))
            s.commit()
            s.refresh(row)
        return row

    def delete(self, schedule_id: int) -> bool:
        with self._sessions() as s:
            row = s.get(StressScheduleModel, schedule_id)
            if row is None:
                return False
            s.delete(row)
            s.commit()
        return True

    def due(self, now: datetime) -> list[StressScheduleModel]:
        """到期调度：enabled 且 next_run_time 非空且 <= now。"""
        stmt = (select(StressScheduleModel)
                .where(StressScheduleModel.enabled == 1)
                .where(StressScheduleModel.next_run_time.is_not(None))
                .where(StressScheduleModel.next_run_time <= now))
        with self._sessions() as s:
            return list(s.scalars(stmt))


class StressScheduleService:
    """调度触发服务：run_due() 扫描到期项并按模板发起压测（同手工链路）。"""

    def __init__(self, repo: StressScheduleRepository, stress_service,
                 poll_interval_sec: float = 30.0):
        self._repo = repo
        self._stress = stress_service
        self._poll_interval_sec = max(5.0, poll_interval_sec)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._trigger_lock = threading.Lock()  # 防多线程重复触发同一批

    @property
    def repo(self) -> StressScheduleRepository:
        return self._repo

    # ------------------------------------------------------------ CRUD 委托

    def create(self, payload: dict) -> StressScheduleModel:
        task = payload.get("task")
        if not isinstance(task, dict) or not task:
            raise ValueError("task（压测任务模板）必填且为 JSON 对象")
        interval = max(1, int(payload.get("intervalMinutes") or DEFAULT_INTERVAL_MIN))
        return self._repo.insert(
            schedule_name=str(payload.get("scheduleName") or "未命名调度"),
            task_json=json.dumps(task, ensure_ascii=False, separators=(",", ":")),
            interval_minutes=interval,
            baseline_task_id=int(payload.get("baselineTaskId") or 0),
            webhook_url=str(payload.get("webhookUrl") or ""),
            enabled=bool(payload.get("enabled", True)),
            next_run_time=datetime.now() + timedelta(minutes=interval),
        )

    def diff(self, schedule_id: int, task_id: int | None = None) -> dict:
        """基线对比：taskId 缺省用上次触发任务；baseline_task_id=0 走自动基线。"""
        from server.baseline_compare import diff as baseline_diff

        schedule = self._repo.find_by_id(schedule_id)
        if schedule is None:
            raise ValueError(f"schedule not found: {schedule_id}")
        current_id = task_id if task_id else schedule.last_task_id
        if not current_id:
            return {"baseline": None, "rows": [], "regressed": False,
                    "noBaseline": True}
        result = baseline_diff(self._stress.repo, int(current_id),
                               int(schedule.baseline_task_id or 0))
        baseline = result.get("baseline")
        return {
            "baseline": baseline.to_camel() if baseline is not None else None,
            "rows": result.get("rows") or [],
            "regressed": bool(result.get("regressed")),
            "noBaseline": bool(result.get("noBaseline")),
        }

    # ------------------------------------------------------------ 触发

    def run_due(self, now: datetime | None = None) -> list[dict]:
        """触发所有到期调度（先推进节拍再发压测：失败不阻塞下一轮）。"""
        now = now or datetime.now()
        triggered: list[dict] = []
        if not self._trigger_lock.acquire(blocking=False):
            return triggered
        try:
            for schedule in self._repo.due(now):
                interval = timedelta(minutes=max(1, schedule.interval_minutes))
                self._repo.update_trigger(
                    schedule.id, last_task_id=schedule.last_task_id,
                    last_run_time=now, next_run_time=now + interval)
                try:
                    task = json.loads(schedule.task_json)
                    replay = self._stress.create_and_run(dict(task))
                    self._repo.update_trigger(
                        schedule.id, last_task_id=int(replay["id"]),
                        last_run_time=now, next_run_time=now + interval)
                    triggered.append({"scheduleId": schedule.id,
                                      "taskId": replay["id"]})
                    if schedule.webhook_url:
                        notify_send(schedule.webhook_url, "[定时压测触发]",
                                    f"{schedule.schedule_name} 已触发任务"
                                    f" #{replay['id']}")
                except Exception as exc:  # noqa: BLE001 — 单项失败不阻断整批
                    log.warning("schedule #%s 触发失败: %s", schedule.id, exc)
                    triggered.append({"scheduleId": schedule.id, "error": str(exc)})
        finally:
            self._trigger_lock.release()
        return triggered

    # ------------------------------------------------------------ 后台线程

    def start(self) -> None:
        """启动守护扫描线程（幂等）；app 装配时调用一次。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(self._poll_interval_sec):
                try:
                    self.run_due()
                except Exception as exc:  # noqa: BLE001 — 常驻线程兜底
                    log.warning("schedule scan failed: %s", exc)

        self._thread = threading.Thread(target=loop, daemon=True,
                                        name="stress-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
