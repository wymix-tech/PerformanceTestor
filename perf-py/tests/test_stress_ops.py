"""P1-3 压测运维控制与 PTDE 数据集联动测试：

- POST /api/stress/tasks/{id}/cancel：运行中可取消（cancelled + finish_time）、
  已结束拒绝、运行器不支持取消拒绝；
- SubprocessRunner：并发槽位限流、watcher 回收临时文件；
- 孤儿收口：create_app 启动时 running/pending 统一置 failed；
- 数据集绑定：production + resetPolicy 拒绝（K5）、非法 resetPolicy 拒绝、
  useSampleCsv 自动注入参数池（用户显式 csv 优先）、after 钩子终局触发；
- 参数样本导出：主表三段分层采样 + manifest 记录。
"""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from engine.result import StressTaskResult
from server.app import create_app
from server.db import make_engine
from server.stress_repo import Base, StressRepository, StressTaskModel
from server.stress_service import InlineRunner, StressTaskService, SubprocessRunner

NOW = datetime(2026, 11, 20, 12, 0, 0)


def _factory() -> sessionmaker:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    return factory


def _seed_task(repo: StressRepository, status: str = "running") -> int:
    with repo._sessions() as s:
        task = StressTaskModel(task_name="ops", target_url="http://demo/api",
                               app_name="demo-app", status=status,
                               create_time=NOW)
        s.add(task)
        s.commit()
        return task.id


def _payload(**over) -> dict:
    payload = {"targetUrl": "http://demo:8081/api", "concurrency": 2, "durationSec": 3}
    payload.update(over)
    return payload


class CancelableRunner:
    """可取消桩：模拟 SubprocessRunner 的 cancel 契约。"""

    def __init__(self):
        self.payloads = []
        self.cancelled = []

    def submit(self, task_id, payload):
        self.payloads.append((task_id, payload))

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        return True


class _EngineStub:
    def run(self, scenario, listener=None):
        return StressTaskResult(
            task_id=0, engine_type="locust", target_url=scenario.request.url,
            concurrency=scenario.load.concurrency, duration_sec=1, warmup_sec=0,
            ramp_up_sec=0, total_requests=5, error_count=0, error_rate=0.0,
            qps=1.0, p50_ms=1.0, p95_ms=1.0, p99_ms=1.0, samples=[],
            stage_points=[], warnings=[])


# ---------------------------------------------------------- cancel API


def test_cancel_running_task_via_api():
    factory = _factory()
    repo = StressRepository(factory)
    runner = CancelableRunner()
    client = TestClient(create_app(stress_repo=repo, stress_runner=runner,
                                   auth_enabled=False))
    task_id = _seed_task(repo, status="running")  # 先建 app 再造任务（避开孤儿收口）
    body = client.post(f"/api/stress/tasks/{task_id}/cancel").json()
    assert body["code"] == 0
    assert body["data"]["status"] == "cancelled"
    assert body["data"]["finishTime"] is not None
    assert runner.cancelled == [task_id]
    with factory() as s:
        row = s.get(StressTaskModel, task_id)
        assert row.status == "cancelled" and row.finish_time is not None


def test_cancel_finished_task_rejected():
    factory = _factory()
    repo = StressRepository(factory)
    task_id = _seed_task(repo, status="finished")
    client = TestClient(create_app(stress_repo=repo, stress_runner=CancelableRunner(),
                                   auth_enabled=False))
    body = client.post(f"/api/stress/tasks/{task_id}/cancel").json()
    assert body["code"] == 1 and "已结束" in body["message"]


def test_cancel_missing_task_rejected():
    client = TestClient(create_app(stress_repo=StressRepository(_factory()),
                                   stress_runner=CancelableRunner(),
                                   auth_enabled=False))
    body = client.post("/api/stress/tasks/999/cancel").json()
    assert body["code"] == 1 and "不存在" in body["message"]


def test_cancel_unsupported_runner_rejected():
    factory = _factory()
    repo = StressRepository(factory)
    client = TestClient(create_app(stress_repo=repo, auth_enabled=False))  # 内存库 InlineRunner
    task_id = _seed_task(repo, status="running")
    body = client.post(f"/api/stress/tasks/{task_id}/cancel").json()
    assert body["code"] == 1 and "不支持取消" in body["message"]
    assert InlineRunner(factory).cancel(task_id) is False


# ---------------------------------------------------------- 并发限流与临时文件


def test_subprocess_runner_rejects_when_slots_exhausted(tmp_path):
    runner = SubprocessRunner("mysql+pymysql://root:pwd@127.0.0.1/db", max_concurrent=1)
    # 槽位被占满（模拟已有一个运行中任务）：第二个提交直接拒绝，且不产生临时文件
    assert runner._slots.acquire(blocking=False)
    before = list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="PERF_STRESS_MAX_CONCURRENT"):
        runner.submit(1, _payload())
    assert list(tmp_path.iterdir()) == before


def test_subprocess_runner_watcher_cleans_payload_and_releases_slot(tmp_path):
    runner = SubprocessRunner("mysql+pymysql://root:pwd@127.0.0.1/db", max_concurrent=1)
    assert runner._slots.acquire(blocking=False)
    payload_file = tmp_path / "perf-py-task-x.json"
    payload_file.write_text("{}", encoding="utf-8")

    class _FakeProc:
        def wait(self):
            return 0

    runner._reap(1, _FakeProc(), str(payload_file))
    assert not payload_file.exists()  # 临时文件已删除
    assert runner._procs == {}
    assert runner._slots.acquire(blocking=False)  # 槽位已释放
    runner._slots.release()


def test_subprocess_runner_unregister_quiet_on_missing_file():
    runner = SubprocessRunner("mysql+pymysql://root:pwd@127.0.0.1/db", max_concurrent=1)
    assert runner._slots.acquire(blocking=False)
    runner._reap(1, type("P", (), {"wait": lambda self: 0})(),
                 "/nonexistent/perf-py-task.json")  # 文件不存在静默跳过


# ---------------------------------------------------------- 孤儿收口


def test_orphan_tasks_failed_on_startup():
    factory = _factory()
    repo = StressRepository(factory)
    running = _seed_task(repo, status="running")
    pending = _seed_task(repo, status="pending")
    finished = _seed_task(repo, status="finished")
    TestClient(create_app(stress_repo=repo, stress_runner=CancelableRunner(),
                          auth_enabled=False))
    with factory() as s:
        assert s.get(StressTaskModel, running).status == "failed"
        assert "服务重启" in s.get(StressTaskModel, running).error_msg
        assert s.get(StressTaskModel, pending).status == "failed"
        assert s.get(StressTaskModel, finished).status == "finished"  # 终态不受影响


# ---------------------------------------------------------- 数据集绑定扩展


def _service(factory, lookup, reset=None, runner=None):
    return StressTaskService(StressRepository(factory),
                             runner or CancelableRunner(),
                             engine_availability=lambda t: True,
                             dataset_lookup=lookup, dataset_reset=reset)


def _ready_lookup(**over) -> dict:
    info = {"rowId": 1, "status": "READY", "environmentId": "staging-mysql",
            "definitionVersion": "v1", "schemaFingerprint": "fp",
            "validationSummary": {"ready": True},
            "sampleCsvPath": None, "sampleCsvRows": 0}
    info.update(over)
    return info


def test_production_dataset_with_reset_policy_rejected():
    factory = _factory()
    svc = _service(factory, lambda iid: _ready_lookup(environmentId="mysql-production"))
    with pytest.raises(ValueError, match="生产环境数据集禁止自动重置"):
        svc.create_and_run(_payload(dataset={"instanceId": "1", "resetPolicy": "after"}))


def test_invalid_reset_policy_rejected():
    factory = _factory()
    svc = _service(factory, lambda iid: _ready_lookup())
    with pytest.raises(ValueError, match="resetPolicy 非法"):
        svc.create_and_run(_payload(dataset={"instanceId": "1", "resetPolicy": "everytime"}))


def test_sample_csv_auto_injected_when_requested(tmp_path):
    factory = _factory()
    csv_file = tmp_path / "1-sample.csv"
    csv_file.write_text("user_id\n1\n2\n", encoding="utf-8")
    svc = _service(factory, lambda iid: _ready_lookup(
        sampleCsvPath=str(csv_file), sampleCsvRows=2))
    runner = CancelableRunner()
    svc._runner = runner
    out = svc.create_and_run(_payload(dataset={"instanceId": "1", "useSampleCsv": True}))
    assert out["id"] > 0
    _, payload = runner.payloads[0]
    assert payload["csv"]["text"] == "user_id\n1\n2\n"
    assert payload["csv"]["mode"] == "sequential"


def test_explicit_csv_wins_over_sample(tmp_path):
    factory = _factory()
    csv_file = tmp_path / "1-sample.csv"
    csv_file.write_text("user_id\n1\n", encoding="utf-8")
    svc = _service(factory, lambda iid: _ready_lookup(sampleCsvPath=str(csv_file)))
    runner = CancelableRunner()
    svc._runner = runner
    svc.create_and_run(_payload(
        csv={"mode": "random", "text": "k\nx\n"},
        dataset={"instanceId": "1", "useSampleCsv": True}))
    _, payload = runner.payloads[0]
    assert payload["csv"]["text"] == "k\nx\n"  # 用户显式参数池优先


def test_use_sample_csv_without_export_rejected():
    factory = _factory()
    svc = _service(factory, lambda iid: _ready_lookup(sampleCsvPath=None))
    with pytest.raises(ValueError, match="无参数样本导出"):
        svc.create_and_run(_payload(dataset={"instanceId": "1", "useSampleCsv": True}))


def test_dataset_binding_wire_shape_unchanged_without_new_fields():
    """未携带新增字段时行为与 T7.3 一致（向后兼容）。"""
    factory = _factory()
    svc = _service(factory, lambda iid: _ready_lookup())
    out = svc.create_and_run(_payload(dataset={"instanceId": "1"}))
    assert out["status"] == "pending"


# ---------------------------------------------------------- reset 钩子


def test_reset_hook_before_and_after(tmp_path):
    factory = _factory()
    phases = []
    csv_file = tmp_path / "1-sample.csv"
    csv_file.write_text("id\n1\n", encoding="utf-8")
    svc = StressTaskService(
        StressRepository(factory), InlineRunner(factory, engine=_EngineStub()),
        engine_availability=lambda t: True,
        dataset_lookup=lambda iid: _ready_lookup(sampleCsvPath=str(csv_file)),
        dataset_reset=lambda iid, phase: phases.append(phase))
    svc.create_and_run(_payload(
        dataset={"instanceId": "1", "resetPolicy": "before_and_after"}))
    assert "before" in phases  # before 同步执行
    deadline = datetime.now().timestamp() + 10
    while "after" not in phases and datetime.now().timestamp() < deadline:
        import time as _t

        _t.sleep(0.2)
    assert "after" in phases  # 任务终局后异步触发


def test_reset_hook_error_does_not_block_task():
    factory = _factory()

    def _boom(iid, phase):
        raise RuntimeError("reset failed")

    svc = StressTaskService(
        StressRepository(factory), CancelableRunner(),
        engine_availability=lambda t: True,
        dataset_lookup=lambda iid: _ready_lookup(),
        dataset_reset=_boom)
    out = svc.create_and_run(_payload(dataset={"instanceId": "1",
                                                "resetPolicy": "before_and_after"}))
    assert out["id"] > 0  # 钩子异常不阻断任务创建


# ---------------------------------------------------------- 参数样本导出


class _FakeCursor:
    def __init__(self, rows_by_query, owner):
        self._rows = rows_by_query
        self._owner = owner

    def execute(self, sql, params=None):  # noqa: ANN001
        self._owner.last_sql = sql
        self._owner.last_params = params

    def fetchone(self):
        sql = self._owner.last_sql
        if "MIN(" in sql:
            return (1, 9)
        raise AssertionError(f"unexpected fetchone: {sql}")

    def fetchall(self):
        sql = self._owner.last_sql
        if "COLUMN_NAME" in sql:
            return [("id", "PRI"), ("user_name", "")]
        if "MIN(" in sql:
            return []
        # 分段采样：按 WHERE 主键区间 + LIMIT 返回段内前 N 行
        seg_lo, seg_hi, per_seg = self._owner.last_params
        upper = min(seg_hi, seg_lo + per_seg - 1)
        return [(i, f"u{i}") for i in range(seg_lo, upper + 1)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self.last_sql = None
        self.last_params = None

    def cursor(self):
        return _FakeCursor({}, self)

    def close(self):
        pass


class _FakeRepo:
    def __init__(self):
        self.definition_id = 7

    def find_definition_by_id(self, definition_id):
        return type("D", (), {"spec_json": json.dumps(
            {"targets": {"rows": {"t_order": {"count": 50000}}}})})()

    @property
    def id(self):
        return 1


def test_export_sample_csv_stratified(tmp_path, monkeypatch):
    from server import dataset_sample

    monkeypatch.setattr(dataset_sample, "resolve_target_dsn",
                        lambda repo: "mysql://root:pwd@127.0.0.1:3306/demo")
    out = dataset_sample.export_sample_csv(
        _FakeRepo(), _FakeRepo(), tmp_path, limit=6, conn_factory=_FakeConn)
    assert out["rows"] == 6
    assert out["entity"] == "t_order"
    assert out["path"].endswith("1-sample.csv")
    lines = (tmp_path / "1-sample.csv").read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "id,user_name"
    assert len(lines) == 7  # header + 6 rows
    assert lines[1].startswith("1,") and lines[6].startswith("6,")


def test_execution_manifest_sample_fields_roundtrip():
    from ptde.models.spec import ExecutionManifest

    m = ExecutionManifest(id="m1", adapter_capability_snapshot={},
                          sample_csv_path="/tmp/1-sample.csv", sample_csv_rows=6)
    data = json.loads(m.model_dump_json())
    assert data["sample_csv_path"] == "/tmp/1-sample.csv"
    assert data["sample_csv_rows"] == 6
