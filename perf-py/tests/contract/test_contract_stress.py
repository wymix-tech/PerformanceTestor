"""stress 域契约测试（阶段 5 · 步骤 2）：Python server 读路径对照 Java golden / 源码契约。

- stress_tasks / stress_task_detail：对照运行中的 Java perf-server 黑盒捕获 golden。
- trend / samples / report：golden 未覆盖（trend 当时误捕获为缺参错误包装），
  以 Java StressController / StressTaskService / 实体源码为契约（键集合与语义），
  待 Java 侧可用时用 capture-contract.sh 重捕 golden 补强。

比对规则同 AI 域：normalize 后相等（键集合与类型一致；标量值不比对）。
"""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.db import make_engine
from server.stress_repo import Base, StressRepository, StressSampleModel, StressTaskModel
from tests.contract._utils import assert_matches_golden, load_golden

URL_USER = "http://localhost:8081/api/user/list?limit=2000"


def _result_json(task_id: int, url: str, qps: float, p50: float, p95: float,
                 p99: float, total: int) -> str:
    """最小压测结果 JSON（字段对齐 Java StressTaskResult 序列化形态）。"""
    return json.dumps({
        "taskId": task_id, "engineType": "http", "targetUrl": url,
        "concurrency": 10, "durationSec": 30, "warmupSec": 3, "rampUpSec": 3,
        "totalRequests": total, "errorCount": 0, "errorRate": 0.0,
        "qps": qps, "p50Ms": p50, "p95Ms": p95, "p99Ms": p99,
        "maxMs": p99 + 30.0, "avgMs": p50,
        "samples": [], "stagePoints": [], "stepStats": [], "warnings": [],
    })


def _task(task_id: int, url: str, create: datetime, finish: datetime,
          app_name: str = "demo-app") -> StressTaskModel:
    return StressTaskModel(
        id=task_id,
        task_name=url,
        engine_type="http",
        target_url=url,
        app_name=app_name,
        app_version="",
        method="GET",
        concurrency=10,
        duration_sec=30,
        qps_limit=0,
        status="finished",
        result_json=_result_json(task_id, url, qps=31.0, p50=320.0,
                                 p95=338.0, p99=347.0, total=930 + task_id),
        error_msg=None,
        create_time=create,
        finish_time=finish,
    )


def _seed(factory: sessionmaker) -> None:
    """种子数据：任务时间倒序（id 大 = 更新），7/8/9 同应用同 URL 供趋势取数。"""
    rows = [
        _task(1, URL_USER, datetime(2026, 11, 13, 18, 27, 57),
              datetime(2026, 11, 13, 12, 29, 2)),
        _task(6, "http://localhost:8081/api/order/recent?limit=200",
              datetime(2026, 11, 13, 18, 32, 24), datetime(2026, 11, 13, 10, 32, 37)),
        _task(7, URL_USER, datetime(2026, 11, 13, 20, 22, 3),
              datetime(2026, 11, 13, 12, 22, 36)),
        _task(8, URL_USER, datetime(2026, 11, 13, 20, 23, 33),
              datetime(2026, 11, 13, 12, 24, 6)),
        _task(9, URL_USER, datetime(2026, 11, 13, 20, 24, 43),
              datetime(2026, 11, 13, 12, 25, 16)),
        _task(10, "http://localhost:8081/api/order/list?status=SHIPPED&limit=500",
              datetime(2026, 11, 13, 20, 26, 16), datetime(2026, 11, 13, 12, 26, 39)),
    ]
    samples = [
        StressSampleModel(id=i, task_id=10,
                          sample_time=datetime(2026, 11, 13, 12, 26, i + 10),
                          concurrency=10, qps=2860.0 + i, p50_ms=3.0,
                          p95_ms=5.0, p99_ms=6.0, error_count=0)
        for i in range(1, 4)
    ]
    with factory() as s:
        s.add_all(rows + samples)
        s.commit()


@pytest.fixture()
def client() -> TestClient:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    _seed(factory)
    app = create_app(stress_repo=StressRepository(factory))
    return TestClient(app)


# ---------------------------------------------------------------- golden 契约

def test_tasks_contract_matches_java(client):
    """/api/stress/tasks?limit=5：ApiResponse 包装 + 任务元素结构对照 golden。"""
    golden = load_golden("stress_tasks")
    resp = client.get("/api/stress/tasks", params={"limit": 5})
    assert_matches_golden(resp.json(), golden, resp.status_code)
    assert resp.json()["data"][0]["id"] == 10, "任务列表按 create_time 倒序"


def test_task_detail_contract_matches_java(client):
    """/api/stress/tasks/{id}：详情结构对照 golden。"""
    resp = client.get("/api/stress/tasks/1")
    assert_matches_golden(resp.json(), load_golden("stress_task_detail"), resp.status_code)


# ---------------------------------------------------------------- 源码级契约

def test_trend_contract_matches_java_source(client):
    """trend：同应用+同 URL 的 finished 任务指标序列（golden 为缺参误捕获，
    契约取自 Java StressTaskService.trend：taskId/taskName/finishTime/qps/p50Ms/
    p95Ms/p99Ms/errorRate/totalRequests，按 finishTime 正序，尾截断保留最近 limit 条）。
    """
    resp = client.get("/api/stress/trend",
                      params={"appName": "demo-app", "url": URL_USER, "limit": 2})
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert len(data) == 2, "只保留最近 limit 条"
    # 同场景任务为 1/7/8/9，按 finishTime 正序尾截断 → 最近两条为 9(12:25:16)、1(12:29:02)
    assert [p["taskId"] for p in data] == [9, 1], "尾截断且保持 finishTime 正序"
    assert set(data[0]) == {"taskId", "taskName", "finishTime", "qps",
                            "p50Ms", "p95Ms", "p99Ms", "errorRate", "totalRequests"}
    assert all(isinstance(p["finishTime"], str) for p in data)


def test_trend_missing_param_matches_golden_error_wrapper(client):
    """缺必填参数 appName：Java 全局异常处理返回 200 + code=1 错误包装
    （stress_trend golden 正是缺参捕获所得，其结构在此成为真实契约断言）。"""
    resp = client.get("/api/stress/trend", params={"url": URL_USER, "limit": 5})
    assert_matches_golden(resp.json(), load_golden("stress_trend"), resp.status_code)
    body = resp.json()
    assert body["code"] == 1 and body["data"] is None
    assert "appName" in body["message"], "message 应指明缺失参数"


def test_samples_contract_matches_java_entity(client):
    """samples：时间升序 + 元素键集合对齐 Java StressSample 实体。"""
    resp = client.get("/api/stress/tasks/10/samples", params={"limit": 500})
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert [s["id"] for s in data] == [1, 2, 3], "sample_time 升序"
    assert set(data[0]) == {"id", "taskId", "sampleTime", "concurrency", "qps",
                            "p50Ms", "p95Ms", "p99Ms", "errorCount"}
    assert all(isinstance(s["sampleTime"], str) for s in data)


def test_report_contract_matches_java_source(client):
    """report：{task, result}，result 为 resultJson 解析产物；任务缺失 → code=1 错误包装
    （对齐 Java ApiResponse.error("stress task not found: " + id)，HTTP 仍 200）。"""
    body = client.get("/api/stress/tasks/10/report").json()
    assert body["code"] == 0
    assert set(body["data"]) == {"task", "result"}
    assert set(body["data"]["task"]) == set(load_golden("stress_tasks")
                                           ["normalized"]["data"][0])
    assert body["data"]["result"]["taskId"] == 10

    missing = client.get("/api/stress/tasks/999/report")
    assert missing.status_code == 200
    assert missing.json() == {"code": 1, "message": "stress task not found: 999",
                              "data": None}


def test_detail_missing_returns_null_data(client):
    """详情任务不存在：data=null（MyBatis findById 空返回 → ApiResponse.ok(null)）。"""
    body = client.get("/api/stress/tasks/999").json()
    assert body["code"] == 0 and body["data"] is None


def test_tasks_status_filter_and_limit_clamp(client):
    """status 过滤 + limit 截断语义对齐 Java list()（1..200）。"""
    body = client.get("/api/stress/tasks", params={"status": "finished", "limit": 2}).json()
    assert body["code"] == 0 and len(body["data"]) == 2
    # limit 超上限截到 200，不报错
    assert client.get("/api/stress/tasks", params={"limit": 500}).json()["code"] == 0
