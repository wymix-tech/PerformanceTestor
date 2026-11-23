"""stress 写路径契约测试（阶段 5 · 步骤 6）：POST /tasks 与 /gate 对照 Java 源码契约。

- wire 形态：ApiResponse 包装 / 任务实体 15 字段（createTime=null 对齐 MyBatis
  插入后未回填）/ GateVerdict 字段集合（taskId/taskName/status/passed/noBaseline/
  regressed/baselineTaskId/rows/summary）与 DiffRow 键集合；
- 生命周期：pending → running → finished，resultJson / stress_sample 落库；
- 门禁：无基线放行/收紧、回归判定文案、显式基线、失败任务。
（本环境 Java server 未运行，POST golden 无法黑盒捕获——以 Java 源码为契约，
Java 侧恢复后可用 capture-contract.sh 扩展 POST 捕获补强，先例同 collector 域。）
"""

import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import server.stress_service as stress_service_mod
from engine.result import Sample, StressTaskResult
from server.app import create_app
from server.db import make_engine
from server.stress_repo import Base, StressRepository
from server.stress_service import InlineRunner, StressTaskService

URL_USER = "http://localhost:8081/api/user/list?limit=2000"


class StubEngine:
    """可编程桩引擎：固定指标输出 + 可选采样/失败。"""

    type = "locust"

    def __init__(self, qps=31.0, p95=338.0, p99=347.0, error_rate=0.0, fail=False):
        self._qps, self._p95, self._p99 = qps, p95, p99
        self._error_rate, self._fail = error_rate, fail

    def run(self, scenario, listener=None):  # noqa: ANN001
        if listener is not None:
            listener(Sample(sample_time=int(time.time() * 1000), concurrency=10,
                            qps=self._qps, p50_ms=320.0, p95_ms=self._p95,
                            p99_ms=self._p99, error_count=1 if self._error_rate else 0))
        if self._fail:
            raise RuntimeError("boom")
        return StressTaskResult(
            engine_type="locust", target_url=scenario.request.url if scenario.request else "",
            concurrency=scenario.load.concurrency, duration_sec=scenario.load.duration_sec,
            warmup_sec=scenario.load.warmup_sec, ramp_up_sec=scenario.load.ramp_up_sec,
            total_requests=int(self._qps * 10), error_count=0,
            error_rate=self._error_rate, qps=self._qps,
            p50_ms=320.0, p95_ms=self._p95, p99_ms=self._p99, max_ms=360.0, avg_ms=320.0)


def _result_json(qps: float, p95: float, p99: float) -> str:
    return json.dumps({"taskId": 0, "engineType": "http", "targetUrl": URL_USER,
                       "concurrency": 10, "durationSec": 30, "warmupSec": 3, "rampUpSec": 3,
                       "totalRequests": 930, "errorCount": 0, "errorRate": 0.0,
                       "qps": qps, "p50Ms": 320.0, "p95Ms": p95, "p99Ms": p99,
                       "maxMs": 360.0, "avgMs": 320.0,
                       "samples": [], "stagePoints": [], "stepStats": [], "warnings": []})


def _seed_baseline(factory: sessionmaker, task_id: int = 7,
                   finish=None, create=None) -> None:
    """种子基线：同应用同 URL 的已完成任务（供 autoBaseline 命中）。"""
    from datetime import datetime

    from server.stress_repo import StressTaskModel

    with factory() as s:
        s.add(StressTaskModel(
            id=task_id, task_name=URL_USER, engine_type="http", target_url=URL_USER,
            app_name="demo-app", app_version="", method="GET", concurrency=10,
            duration_sec=30, qps_limit=0, status="finished",
            result_json=_result_json(100.0, 50.0, 60.0), error_msg=None,
            create_time=create or datetime(2026, 11, 15, 10, 0, 0),
            finish_time=finish or datetime(2026, 11, 15, 10, 1, 0)))
        s.commit()


@pytest.fixture()
def db_url(tmp_path) -> str:
    """每测试独立文件库：worker 线程与轮询会话并发使用同一 StaticPool 单连接
    （内存库）会出现事务可见性竞争，文件库多连接语义与生产 MySQL 一致。"""
    return f"sqlite:///{tmp_path}/stress.db"


def _make_client(engine, seed_baseline: bool = True, db_url: str = "sqlite:///:memory:") -> TestClient:
    factory = sessionmaker(bind=make_engine(db_url), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    if seed_baseline:
        _seed_baseline(factory)
    runner = InlineRunner(factory, engine=engine)
    app = create_app(stress_repo=StressRepository(factory), auth_enabled=False,
                     stress_runner=runner)
    # 引擎可用性桩：测试环境不依赖 locust 安装
    app.state.stress_service = StressTaskService(
        app.state.stress_repo, runner, engine_availability=lambda t: True)
    return TestClient(app)


def _post_tasks(client: TestClient, **overrides) -> dict:
    payload = {"targetUrl": URL_USER, "appName": "demo-app",
               "concurrency": 10, "durationSec": 1, "warmupSec": 0, **overrides}
    return client.post("/api/stress/tasks", json=payload).json()


def _await_status(client: TestClient, task_id: int, want=("finished",),
                  timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/stress/tasks/{task_id}").json()
        status = (body.get("data") or {}).get("status")
        if status in want:
            return status
        time.sleep(0.05)
    return "pending"


# ---------------------------------------------------------------- POST /tasks


def test_start_response_wire_shape(db_url):
    """/tasks 响应：任务实体 15 字段，createTime=null（对齐 MyBatis 插入后未回填）。"""
    client = _make_client(StubEngine(), db_url=db_url)
    body = _post_tasks(client)
    assert body["code"] == 0 and body["message"] == "success"
    data = body["data"]
    assert set(data) == {"id", "taskName", "engineType", "targetUrl", "method",
                         "appName", "appVersion", "concurrency", "durationSec",
                         "qpsLimit", "status", "resultJson", "errorMsg",
                         "createTime", "finishTime"}
    assert data["status"] == "pending" and data["createTime"] is None
    assert data["engineType"] == "http" and data["method"] == "GET"
    assert data["targetUrl"] == URL_USER


def test_start_lifecycle_result_and_sample_persisted(db_url):
    """/tasks 生命周期：pending → running → finished，resultJson/采样点落库。"""
    client = _make_client(StubEngine(), db_url=db_url)
    task_id = _post_tasks(client)["data"]["id"]
    assert _await_status(client, task_id) == "finished"
    detail = client.get(f"/api/stress/tasks/{task_id}").json()["data"]
    result = json.loads(detail["resultJson"])
    assert result["qps"] == 31.0 and result["p95Ms"] == 338.0
    assert result["taskId"] == task_id
    # 单档模式补齐 stagePoints（对齐 Java HttpStressEngine 单档分支）
    assert len(result["stagePoints"]) == 1 and result["stagePoints"][0]["qps"] == 31.0
    assert detail["finishTime"] is not None
    samples = client.get(f"/api/stress/tasks/{task_id}/samples").json()["data"]
    assert len(samples) == 1 and samples[0]["taskId"] == task_id
    assert samples[0]["qps"] == 31.0


def test_start_failed_task_error_msg(db_url):
    """/tasks 失败路径：status=failed + errorMsg（对齐 updateFailed）。"""
    client = _make_client(StubEngine(fail=True), db_url=db_url)
    task_id = _post_tasks(client)["data"]["id"]
    assert _await_status(client, task_id, want=("finished", "failed")) == "failed"
    detail = client.get(f"/api/stress/tasks/{task_id}").json()["data"]
    assert detail["errorMsg"] == "boom" and detail["resultJson"] is None
    assert detail["finishTime"] is not None


def test_start_scenario_mode_task_name_fallback(db_url):
    """场景模式任务名/URL 摘要：对齐 createAndRun 的 fallbackName。"""
    client = _make_client(StubEngine(), db_url=db_url)
    body = _post_tasks(client, targetUrl=None, scenario=[
        {"url": "http://x/login", "weight": 1},
        {"url": "http://x/list", "weight": 1},
    ])
    data = body["data"]
    assert data["taskName"] == "场景压测（2 步）"
    assert data["targetUrl"] == "场景压测（2 步）"


def test_start_validation_error_wire_exact(db_url):
    """/tasks 校验失败：code=1 + 文案原样（无内部错误前缀，HTTP 200）。"""
    client = _make_client(StubEngine(), db_url=db_url)
    resp = client.post("/api/stress/tasks", json={"targetUrl": "ftp://x"})
    assert resp.status_code == 200
    assert resp.json() == {"code": 1, "message": "压测目标 URL 必须以 http:// 或 https:// 开头",
                           "data": None}
    # 并发越界
    resp2 = client.post("/api/stress/tasks",
                        json={"targetUrl": URL_USER, "concurrency": 0})
    assert resp2.json()["message"] == "并发数必须在 1-1000 之间"


def test_start_app_version_snapshot(db_url):
    """/tasks 版本快照：appVersion 取 app_instance 心跳值（缺省空串）。"""
    client = _make_client(StubEngine(), db_url=db_url)
    assert _post_tasks(client)["data"]["appVersion"] == ""


# ---------------------------------------------------------------- POST /gate


def test_gate_no_baseline_pass_by_default(db_url):
    """无基线默认放行：文案逐字对齐 StressGateService。"""
    client = _make_client(StubEngine(qps=31.0), seed_baseline=False, db_url=db_url)
    body = client.post("/api/stress/gate", json={
        "targetUrl": URL_USER, "appName": "demo-app", "concurrency": 10,
        "durationSec": 1, "warmupSec": 0}).json()
    assert body["code"] == 0
    v = body["data"]
    assert set(v) == {"taskId", "taskName", "status", "passed", "noBaseline",
                      "regressed", "baselineTaskId", "rows", "summary",
                      "verdict", "baselineMode", "types"}  # P2-3 verdict 枚举演进
    assert v["status"] == "finished" and v["noBaseline"] is True
    assert v["passed"] is True and v["baselineTaskId"] == 0
    assert v["summary"] == "无可用基线（首次执行或历史任务缺失），本次结果已记录为后续基线，门禁放行"


def test_gate_no_baseline_fail_when_strict(db_url):
    client = _make_client(StubEngine(), seed_baseline=False, db_url=db_url)
    v = client.post("/api/stress/gate",
                    params={"failOnNoBaseline": "true"},
                    json={"targetUrl": URL_USER, "appName": "demo-app",
                          "concurrency": 10, "durationSec": 1, "warmupSec": 0}).json()["data"]
    assert v["passed"] is False
    assert v["summary"] == "无可用基线，按 fail-on-no-baseline 策略判定为失败"


def test_gate_regression_detected_and_message_exact(db_url):
    """autoBaseline 命中 + QPS 下降超 10%：回归判定与文案逐字对齐。"""
    client = _make_client(StubEngine(qps=80.0, p95=52.0, p99=61.0), db_url=db_url)
    v = client.post("/api/stress/gate", json={
        "targetUrl": URL_USER, "appName": "demo-app", "concurrency": 10,
        "durationSec": 1, "warmupSec": 0}).json()["data"]
    assert v["regressed"] is True and v["passed"] is False
    assert v["baselineTaskId"] == 7
    # P2-3 演进：summary 携带 baselineMode 与回归类型标注（verdict 枚举同步新增）
    assert v["summary"] == ("性能回归：与基线任务 #7（latest）相比，"
                            "QPS 100 → 80（-20.0%）（回归类型：throughput=regressed）")
    assert v["verdict"] == "regressed" and v["baselineMode"] == "latest"
    labels = [r["label"] for r in v["rows"]]
    assert labels == ["QPS", "P95 (ms)", "P99 (ms)", "错误率 (%)"]
    qps_row = v["rows"][0]
    assert qps_row == {"label": "QPS", "base": "100", "cur": "80",
                       "deltaPct": "-20.0%", "improved": False, "regressed": True}


def test_gate_pass_no_regression(db_url):
    client = _make_client(StubEngine(qps=98.0, p95=52.0, p99=61.0), db_url=db_url)
    v = client.post("/api/stress/gate", json={
        "targetUrl": URL_USER, "appName": "demo-app", "concurrency": 10,
        "durationSec": 1, "warmupSec": 0}).json()["data"]
    assert v["passed"] is True and v["regressed"] is False
    assert v["summary"] == "性能达标：与基线任务 #7（latest）相比无回归"


def test_gate_error_rate_rise_hits_regression_via_diff_fallback(db_url):
    """错误率行 regressed 恒 False，但 errC-errB>5 由 diff 兜底判回归（Java 现状语义）。"""
    client = _make_client(StubEngine(qps=99.0, p95=52.0, p99=61.0, error_rate=10.0), db_url=db_url)
    v = client.post("/api/stress/gate", json={
        "targetUrl": URL_USER, "appName": "demo-app", "concurrency": 10,
        "durationSec": 1, "warmupSec": 0}).json()["data"]
    assert v["regressed"] is True and v["passed"] is False
    assert "错误率上升超阈值" in v["summary"], "无指标行回归时文案走兜底分支"
    assert v["types"]["stability"] == "regressed"


def test_gate_explicit_baseline_task_id(db_url):
    """显式 baselineTaskId：直接取该任务为基线。"""
    client = _make_client(StubEngine(qps=80.0), db_url=db_url)
    v = client.post("/api/stress/gate",
                    params={"baselineTaskId": "7"},
                    json={"targetUrl": URL_USER, "appName": "other-app",
                          "concurrency": 10, "durationSec": 1, "warmupSec": 0}).json()["data"]
    assert v["baselineTaskId"] == 7 and v["regressed"] is True


def test_gate_failed_task_summary(db_url):
    """压测本体失败：status=failed + 未完成文案（对齐 awaitFinish 分支）。"""
    client = _make_client(StubEngine(fail=True), seed_baseline=False, db_url=db_url)
    v = client.post("/api/stress/gate", json={
        "targetUrl": URL_USER, "appName": "demo-app", "concurrency": 10,
        "durationSec": 1, "warmupSec": 0}).json()["data"]
    assert v["status"] == "failed" and v["passed"] is False
    assert v["summary"].startswith("压测未在 ") and v["summary"].endswith("（状态：failed）")


def test_gate_notify_webhook_on_failure(db_url, monkeypatch):
    """失败时告警尽力而为；通过/未配置 webhook 不发送。"""
    sent = []
    monkeypatch.setattr(stress_service_mod, "notify_send",
                        lambda url, title, text: sent.append((url, title, text)))
    client = _make_client(StubEngine(qps=80.0), db_url=db_url)
    client.post("/api/stress/gate", params={"webhook": "http://hook.example/x"},
                json={"targetUrl": URL_USER, "appName": "demo-app",
                      "concurrency": 10, "durationSec": 1, "warmupSec": 0})
    assert len(sent) == 1
    assert sent[0][0] == "http://hook.example/x"
    assert sent[0][1].startswith("性能门禁未通过 #")
    assert "QPS 100 → 80" in sent[0][2]


def test_gate_missing_body_error_wrapper(db_url):
    """缺请求体：FastAPI 校验失败 → 全局异常处理器形态（code=1 + 前缀）。"""
    client = _make_client(StubEngine(), db_url=db_url)
    resp = client.post("/api/stress/gate")
    assert resp.status_code == 200
    assert resp.json()["code"] == 1


# ---------------------------------------------------------------- 门禁超时估算


def test_gate_min_timeout_uses_raw_stages_only(db_url):
    """estimateMinTimeout 对齐 Java：用原始 stages，stageConcurrencies 简写不计入。"""
    from server.stress_request import parse_stress_request
    from server.stress_service import GATE_TIMEOUT_OVERHEAD_SEC, StressTaskService

    r = parse_stress_request({"durationSec": 10, "warmupSec": 2, "rampUpSec": 1,
                              "stageConcurrencies": [10, 20, 30]})
    assert StressTaskService._estimate_min_timeout(r) == \
        10 + 2 + 1 + GATE_TIMEOUT_OVERHEAD_SEC
    r2 = parse_stress_request({"durationSec": 10, "stages": [
        {"concurrency": 1, "durationSec": 5}, {"concurrency": 2, "durationSec": 6}]})
    # warmupSec 走字段默认 10：10 + 10 + 5 + 6 + 60
    assert StressTaskService._estimate_min_timeout(r2) == 10 + 10 + 5 + 6 + GATE_TIMEOUT_OVERHEAD_SEC
