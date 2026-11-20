"""容量评估路由测试：POST /api/capacity/analyze → result_json 回写 →
GET /api/stress/tasks/{id}/report 回查 capacity → GET /api/capacity/advice。

覆盖前端 CapacityAdvicePanel.vue 消费的 advices / jvmAdvice 字段。
"""

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.collector_repo import CollectorRepository, JvmMetricModel
from server.db import make_engine
from server.stress_repo import Base, StressRepository, StressTaskModel

STAGE_POINTS = [
    {"concurrency": 10, "qps": 180, "p50Ms": 20, "p95Ms": 120, "p99Ms": 180,
     "errorRate": 0.0, "totalRequests": 1800},
    {"concurrency": 20, "qps": 300, "p50Ms": 30, "p95Ms": 210, "p99Ms": 320,
     "errorRate": 0.0, "totalRequests": 3000},
    {"concurrency": 40, "qps": 420, "p50Ms": 40, "p95Ms": 380, "p99Ms": 500,
     "errorRate": 0.01, "totalRequests": 4200},
    {"concurrency": 80, "qps": 430, "p50Ms": 90, "p95Ms": 900, "p99Ms": 1500,
     "errorRate": 0.12, "totalRequests": 4300},
]


def _factory() -> sessionmaker:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    return factory


def _seed_task(factory, app_name="demo-app") -> int:
    with factory() as s:
        task = StressTaskModel(
            task_name="下单接口压测", target_url="http://demo/order", app_name=app_name,
            status="finished", create_time=datetime.now(), finish_time=datetime.now(),
            result_json=json.dumps({"qps": 420, "p95Ms": 380, "stagePoints": STAGE_POINTS},
                                   ensure_ascii=False))
        s.add(task)
        s.commit()
        return task.id


def _seed_jvm(factory, app_name="demo-app", heap_used=900, heap_max=1024):
    now = datetime.now()
    with factory() as s:
        s.add_all([
            JvmMetricModel(app_name=app_name, collect_time=now - timedelta(minutes=i),
                           heap_used_mb=heap_used, heap_max_mb=heap_max)
            for i in range(3)
        ])
        s.commit()


@pytest.fixture()
def ctx():
    factory = _factory()
    task_id = _seed_task(factory)
    client = TestClient(create_app(stress_repo=StressRepository(factory),
                                   collector_repo=CollectorRepository(factory),
                                   auth_enabled=False))
    return client, factory, task_id


def test_analyze_wire_and_advices(ctx):
    """拟合结果 + advices + 回写 report；目标 P95 传 epoch 兼容形态数字。"""
    client, _, task_id = ctx
    body = client.post("/api/capacity/analyze",
                       json={"taskId": task_id, "targetP95Ms": 500}).json()
    assert body["code"] == 0
    cap = body["data"]
    assert cap["maxQps"] > 0 and cap["maxConcurrency"] > 0
    assert cap["targetP95Ms"] == 500.0
    assert cap["advices"] and set(cap["advices"][0]) >= {
        "title", "description", "scaleType", "riskLevel"}
    # 未触达 500ms 拐点 → 容量探测建议（low）
    assert cap["advices"][0]["scaleType"] == "容量探测"

    # 回写后 report 自动带出 capacity
    report = client.get(f"/api/stress/tasks/{task_id}/report").json()["data"]
    assert report["result"]["capacity"]["maxQps"] == cap["maxQps"]
    # 原始 stagePoints 不丢失
    assert len(report["result"]["stagePoints"]) == len(STAGE_POINTS)


def test_analyze_jvm_advice_and_report_chain(ctx):
    """最近 1 小时 JVM 峰值 → jvmAdvice（峰值占比 ≥80% → 堆扩容 + 高风险 advice）。"""
    client, factory, task_id = ctx
    _seed_jvm(factory, heap_used=900, heap_max=1024)  # 87.9%
    body = client.post("/api/capacity/analyze", json={"taskId": task_id}).json()["data"]
    jvm = body["jvmAdvice"]
    assert jvm["peakHeapRatio"] >= 80
    assert "-Xmx" in jvm["heapSize"] and jvm["gcCollector"].startswith("G1")
    assert "-XX:+UseG1GC" in jvm["params"]
    assert any(a["scaleType"] == "垂直调优" and a["riskLevel"] == "high"
               for a in body["advices"])


def test_analyze_task_not_found(ctx):
    client, _, _ = ctx
    body = client.post("/api/capacity/analyze", json={"taskId": 99999}).json()
    assert body["code"] == 1 and "not found" in body["message"]


def test_advice_returns_latest_evaluated_task(ctx):
    client, _, task_id = ctx
    client.post("/api/capacity/analyze", json={"taskId": task_id, "targetP95Ms": 500})
    # 按 app 命中
    data = client.get("/api/capacity/advice", params={"app": "demo-app"}).json()["data"]
    assert data["taskId"] == task_id and data["appName"] == "demo-app"
    assert data["capacity"]["advices"]
    # 其它应用无已评估任务 → capacity=None 非错误
    miss = client.get("/api/capacity/advice", params={"app": "ghost"}).json()["data"]
    assert miss["taskId"] is None and miss["capacity"] is None
