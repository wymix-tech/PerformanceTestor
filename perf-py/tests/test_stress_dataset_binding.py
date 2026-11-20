"""T7.3 压测绑定单测：stress_task 加列快照 + READY 门禁 + 反查链路。

- 创建压测任务携带 dataset.instanceId → 仅 READY 实例可绑定（非 READY/
  不存在/查询缝未注入显式拒绝）；绑定快照（Definition 版本/Schema 指纹/
  验证结果摘要）随任务落库；
- GET /api/stress/tasks/{id}/dataset 反查：压测报告 → DatasetInstance →
  Definition 版本 + Schema 指纹 + 验证结果全链可查（实施方案 §10.3 门禁 2）；
- 未绑定任务的 wire 契约不变（既有 to_camel 字段零改动）。
"""

import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.db import make_engine
from server.ptde_repo import PtdeDatasetRepository
from tests.test_server_dataset_api import MINIMAL_SPEC, FakePipeline


def _client(monkeypatch=None) -> TestClient:
    if monkeypatch is not None:
        # 引擎可用性探测绕开 locust 导入闸门（conftest gevent gate）
        monkeypatch.setattr("server.stress_service.engine_available", lambda t: True)
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    import server.collector_repo  # noqa: F401
    import server.ptde_repo  # noqa: F401
    import server.stress_repo  # noqa: F401  stress/collector/ptde 模型统一注册
    from server.db import Base
    from server.stress_repo import StressRepository

    Base.metadata.create_all(factory.kw["bind"])
    app = create_app(dataset_repo=PtdeDatasetRepository(factory),
                     stress_repo=StressRepository(factory),
                     dataset_pipeline=FakePipeline(),
                     stress_runner=type("NoopRunner", (), {"submit": lambda *a: None})(),
                     auth_enabled=False)
    return TestClient(app)


def _ready_instance(client: TestClient, environment: str = "dev-1") -> str:
    """注册 Definition → 建 Instance → 异步构建至 READY，返回实例 ID。"""
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": environment}
    ).json()["data"]["id"]
    client.post(f"/api/datasets/{iid}/build")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        data = client.get(f"/api/datasets/{iid}/progress").json()["data"]
        if not data["running"]:
            break
        time.sleep(0.02)
    assert data["instanceStatus"] == "READY"
    return iid


def _create_stress_task(client: TestClient, dataset: dict | None = None) -> dict:
    payload = {
        "targetUrl": "http://target-host:8080/api/orders",
        "concurrency": 10, "durationSec": 5,
    }
    if dataset is not None:
        payload["dataset"] = dataset
    return client.post("/api/stress/tasks", json=payload).json()


def test_bind_ready_dataset_snapshot_recorded(monkeypatch):
    client = _client(monkeypatch)
    iid = _ready_instance(client)
    resp = _create_stress_task(client, {"instanceId": iid})
    assert resp["code"] == 0, resp
    task_id = resp["data"]["id"]
    # 反查端点：压测任务 → 数据集全链
    chain = client.get(f"/api/stress/tasks/{task_id}/dataset").json()["data"]
    assert chain["datasetInstanceId"] == iid
    assert chain["datasetDefinitionVersion"] == "1"
    assert chain["datasetSchemaFingerprint"] is None or isinstance(
        chain["datasetSchemaFingerprint"], str)
    snapshot = chain["datasetValidation"]
    assert snapshot and snapshot["ready"] is True  # 绑定时验证结果摘要留痕
    # 全链可查：dataset 域详情（Instance → Definition → 报告）
    assert chain["dataset"]["definitionId"] == "orders@1"
    assert len(chain["dataset"]["reports"]) == 5


def test_bind_non_ready_dataset_rejected(monkeypatch):
    client = _client(monkeypatch)
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"}
    ).json()["data"]["id"]  # PLANNED（未构建）
    resp = _create_stress_task(client, {"instanceId": iid})
    assert resp["code"] == 1
    assert "非 READY" in resp["message"]


def test_bind_unknown_dataset_rejected(monkeypatch):
    client = _client(monkeypatch)
    resp = _create_stress_task(client, {"instanceId": "999"})
    assert resp["code"] == 1
    assert "不存在" in resp["message"]


def test_bind_without_dataset_lookup_rejected():
    """查询缝未注入（直建 service）→ 携带绑定请求显式拒绝，不静默忽略。"""
    from sqlalchemy.orm import sessionmaker as sm

    from server.db import make_engine
    from server.stress_repo import StressRepository
    from server.stress_service import StressTaskService

    factory = sm(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    repo = StressRepository(factory)
    svc = StressTaskService(repo, runner=type("R", (), {"submit": lambda *a: None})())
    with pytest.raises(ValueError, match="未装配"):
        svc._resolve_dataset_binding({"dataset": {"instanceId": "1"}})


def test_unbound_task_wire_contract_unchanged(monkeypatch):
    client = _client(monkeypatch)
    resp = _create_stress_task(client)
    assert resp["code"] == 0
    data = resp["data"]
    # 契约字段零改动（dataset_* 不进 wire 形态）
    assert "datasetInstanceId" not in data
    assert {"id", "taskName", "status", "concurrency", "durationSec"} <= set(data)
    # 反查端点：未绑定 → data=null（非错误）
    assert client.get(f"/api/stress/tasks/{data['id']}/dataset").json()["data"] is None


def test_dataset_binding_endpoint_unknown_task_error(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/stress/tasks/999/dataset").json()
    assert resp["code"] == 1


def _scenario_payload(dataset: dict | None = None) -> dict:
    """场景模式请求体（scenario 非空列表即场景模式，无 targetUrl）。"""
    payload = {
        "taskName": "场景绑定压测",
        "scenario": [
            {"name": "s1", "url": "http://target-host:8080/api/orders", "weight": 2},
            {"name": "s2", "url": "http://target-host:8080/api/users", "weight": 1},
        ],
        "concurrency": 5, "durationSec": 5,
    }
    if dataset is not None:
        payload["dataset"] = dataset
    return payload


def test_bind_ready_dataset_scenario_mode_snapshot_recorded(monkeypatch):
    """场景模式与单接口同一条绑定路径：payload.dataset.instanceId → READY 快照落库。

    `_resolve_dataset_binding` 直读原始 payload（与 is_scenario_mode 无关），
    本例逐项核对场景路径：绑定快照 + 反查链路 + wire 契约零改动全部一致。
    """
    client = _client(monkeypatch)
    iid = _ready_instance(client)
    resp = client.post("/api/stress/tasks", json=_scenario_payload({"instanceId": iid})).json()
    assert resp["code"] == 0, resp
    data = resp["data"]
    # 场景模式任务形态：URL 取场景摘要，绑定快照不进 wire（契约零改动）
    assert "datasetInstanceId" not in data
    chain = client.get(f"/api/stress/tasks/{data['id']}/dataset").json()["data"]
    assert chain["datasetInstanceId"] == iid
    assert chain["datasetDefinitionVersion"] == "1"
    snapshot = chain["datasetValidation"]
    assert snapshot and snapshot["ready"] is True
    # 全链可查：Instance → Definition → 五层报告（与非场景模式一致）
    assert chain["dataset"]["definitionId"] == "orders@1"
    assert len(chain["dataset"]["reports"]) == 5


def test_bind_non_ready_dataset_rejected_scenario_mode(monkeypatch):
    """场景模式下 READY 门禁不放宽：PLANNED 实例绑定显式拒绝（§9.5 门禁 5）。"""
    client = _client(monkeypatch)
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"}
    ).json()["data"]["id"]  # PLANNED（未构建）
    resp = client.post(
        "/api/stress/tasks", json=_scenario_payload({"instanceId": iid})).json()
    assert resp["code"] == 1
    assert "非 READY" in resp["message"]


def test_scenario_unbound_task_no_binding(monkeypatch):
    """场景模式不带 dataset → 不绑定（反查 data=null），与非场景模式一致。"""
    client = _client(monkeypatch)
    resp = client.post("/api/stress/tasks", json=_scenario_payload()).json()
    assert resp["code"] == 0
    assert client.get(
        f"/api/stress/tasks/{resp['data']['id']}/dataset").json()["data"] is None


def test_binding_json_roundtrip_via_repo():
    from sqlalchemy.orm import sessionmaker as sm

    from server.db import make_engine
    from server.stress_repo import StressRepository

    factory = sm(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    import server.stress_repo  # noqa: F401
    from server.db import Base

    Base.metadata.create_all(factory.kw["bind"])
    repo = StressRepository(factory)
    tid = repo.insert_task(
        task_name="t", engine_type="http", target_url="http://x", app_name="",
        app_version="", method="GET", concurrency=1, duration_sec=1, qps_limit=0,
        dataset_instance_id=7, dataset_definition_version="2",
        dataset_schema_fingerprint="ab" * 32,
        dataset_validation_json=json.dumps({"ready": True}))
    binding = repo.dataset_binding(tid)
    assert binding == {
        "taskId": tid, "datasetInstanceId": "7",
        "datasetDefinitionVersion": "2",
        "datasetSchemaFingerprint": "ab" * 32,
        "datasetValidation": {"ready": True},
    }
    assert repo.dataset_binding(9999) is None
