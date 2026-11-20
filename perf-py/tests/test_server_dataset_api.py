"""B7 数据集生命周期落库层 + 服务 + /api/datasets/* 端点单测。

- 落库层：三表读写、id 映射（DB 行主键 ↔ ptde str id）、报告聚合；
- 服务：注册唯一性/实例创建/状态机流转落库/异步构建编排/READY 终局/
  清理决策与授权/生产环境拒绝；
- API：端点形状 + code=1 错误形态 + 异步进度查询。

执行体用 FakePipeline 注入（generate/load/verify 三段缝；真库端到端执行体
随 A2 真库环境批次接线——B6 §7F 口径）。
"""

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.dataset_service import (
    DatasetCancelled,
    DatasetLifecycleService,
    DatasetServiceError,
)
from server.db import make_engine
from server.ptde_repo import PtdeDatasetRepository

# ---------------------------------------------------------------- 夹具

MINIMAL_SPEC = {
    "name": "订单数据集",
    "targets": {"rows": {"t_order": 100}},
}


class FakePipeline:
    """三段执行体替身：generate/load 汇总 + 全要素 manifest/五层报告/清理上下文。"""

    def __init__(self, *, fail_at: str | None = None, ready: bool = True):
        self.fail_at = fail_at
        self.ready = ready
        self.calls: list[tuple[str, str]] = []

    def generate(self, ctx):
        iid = ctx["instance"]["id"]
        self.calls.append(("generate", iid))
        if self.fail_at == "generate":
            raise RuntimeError("生成失败注入")
        return {"generated_rows": 100}

    def load(self, ctx):
        iid = ctx["instance"]["id"]
        self.calls.append(("load", iid))
        if self.fail_at == "load":
            raise RuntimeError("装载失败注入")
        return {"loaded_rows": 100}

    def verify(self, ctx):
        iid = ctx["instance"]["id"]
        self.calls.append(("verify", iid))
        if self.fail_at == "verify":
            raise RuntimeError("验证失败注入")
        status = "PASS" if self.ready else "FAIL"
        return {
            "report": {
                "instance_id": iid,
                "layers": [
                    {"layer": layer, "status": status,
                     "metrics": {"tables": 1}, "checks": [], "evidence": ["e1"]}
                    for layer in ("count", "relationship", "distribution",
                                  "business", "db_ready")
                ],
            },
            "manifest": _manifest(iid, ready=self.ready),
            "cleanup": {"env_class": "dedicated_test_db"},
        }

    def cleanup_executor(self):
        def executor(tier):
            self.calls.append(("cleanup", tier.value))
            return {"deleted": 0}

        return executor


def _manifest(iid: str, *, ready: bool = True) -> dict:
    return {
        "id": "m-1",
        "instance_id": iid,
        "seed": 42,
        "generator_version": "1.2.0",
        "partition_algorithm_version": "1",
        "model_fingerprints": {
            "spec": "s-fp", "plan": "p-fp", "profile": "pr-fp", "schema": "sc-fp"},
        "adapter_capability_snapshot": {"mysql": {"bulkload": "LOAD_DATA"}},
        "adapter_version": "1.0",
        "actual_id_ranges": {"t_order": {"start": 1, "end": 100}},
        "shard_summaries": [{
            "shard_id": "t_order#0", "entity": "t_order",
            "generated_rows": 100,
            "loaded_rows": 100 if ready else 99,
            "status": "loaded" if ready else "loaded",
        }],
    }


def _make_service(pipeline=None) -> DatasetLifecycleService:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    import server.ptde_repo  # noqa: F401  注册模型
    from server.db import Base

    Base.metadata.create_all(factory.kw["bind"])
    return DatasetLifecycleService(
        PtdeDatasetRepository(factory), pipeline=pipeline or FakePipeline())


def _wait_running_done(service, instance_id: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.task_status(instance_id)
        if not status["running"]:
            return service.get_instance(instance_id)
        time.sleep(0.02)
    raise AssertionError("异步任务超时未完成")


def _make_client(pipeline=None) -> TestClient:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    import server.ptde_repo  # noqa: F401  注册模型
    from server.db import Base

    Base.metadata.create_all(factory.kw["bind"])
    app = create_app(
        dataset_repo=PtdeDatasetRepository(factory),
        dataset_pipeline=pipeline or FakePipeline(),
        stress_runner=type("NoopRunner", (), {"submit": lambda *a: None})(),
        auth_enabled=False)
    return TestClient(app)


# ---------------------------------------------------------------- 落库层 repo


def test_repo_definition_roundtrip():
    svc = _make_service()
    data = svc.register_definition("orders", "1", "订单数据集", MINIMAL_SPEC)
    assert data["defKey"] == "orders"
    assert data["specFingerprint"]  # 注册时指纹封存
    assert svc.list_versions("orders") == ["1"]


def test_repo_duplicate_definition_rejected():
    svc = _make_service()
    svc.register_definition("orders", "1", "订单数据集", MINIMAL_SPEC)
    with pytest.raises(DatasetServiceError, match="已存在"):
        svc.register_definition("orders", "1", "订单数据集", MINIMAL_SPEC)


def test_repo_instance_id_maps_to_db_row_pk():
    svc = _make_service()
    svc.register_definition("orders", "1", "订单数据集", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    assert inst["id"].isdigit()  # DB 自增主键字符串形态（链路可追溯）
    assert inst["status"] == "PLANNED"


def test_instance_owner_token_generated():
    svc = _make_service()
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    row = svc._repo.find_instance(int(inst["id"]))
    assert row.owner_token and len(row.owner_token) == 32


def test_repo_report_roundtrip():
    svc = _make_service()
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    from ptde.models.dataset import LayerKind, LayerResult, ValidationStatus

    svc._repo.report_from_layer(
        int(inst["id"]),
        LayerResult(layer=LayerKind.COUNT, status=ValidationStatus.WARN,
                    metrics={"a": 1}, tolerance={"relative": 0}, evidence=["e"]))
    report = svc._repo.report_to_model(inst["id"], svc._repo.reports_for_instance(int(inst["id"])))
    assert report.instance_id == inst["id"]
    assert report.layers[0].status is ValidationStatus.WARN
    assert report.layers[0].tolerance == {"relative": 0}


# ---------------------------------------------------------------- 服务层：构建编排


def test_build_happy_path_transitions_to_ready():
    pipeline = FakePipeline()
    svc = _make_service(pipeline)
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    accepted = svc.build(inst["id"])
    assert accepted["accepted"] is True
    final = _wait_running_done(svc, inst["id"])
    assert final["status"] == "READY"
    assert [c[0] for c in pipeline.calls] == ["generate", "load", "verify"]
    # 状态机审计：全链转换记录落库（可追溯）
    names = [(t["from_state"], t["to_state"]) for t in final["transitionLog"]]
    assert ("PLANNED", "GENERATING") in names
    assert ("VERIFYING", "READY") in names
    # 五层报告落 ptde_validation_report
    reports = svc.instance_reports(inst["id"])
    assert [r["layer"] for r in reports] == [
        "count", "relationship", "distribution", "business", "db_ready"]
    # READY 八项判定结果入 validation_summary
    assert final["validationSummary"]["ready"] is True
    # ExecutionManifest 落 ptde_execution_manifest（B7 续：READY 条件 7 持久化侧）
    summary = svc.instance_manifest(inst["id"])
    assert summary["instanceId"] == inst["id"]
    assert summary["fingerprints"]["spec"] == "s-fp"
    assert summary["shardSummaries"][0]["status"] == "loaded"


def test_instance_manifest_requires_build():
    svc = _make_service()
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    with pytest.raises(DatasetServiceError, match="无执行清单"):
        svc.instance_manifest(inst["id"])


def test_build_not_ready_fails_via_state_machine():
    svc = _make_service(FakePipeline(ready=False))
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    svc.build(inst["id"])
    final = _wait_running_done(svc, inst["id"])
    assert final["status"] == "FAILED"
    assert final["validationSummary"]["ready"] is False
    failed = [c for c in final["validationSummary"]["checks"] if c["status"] == "FAIL"]
    assert any(c["name"] == "ready.shards_complete" for c in failed)
    assert final["errorMsg"] is None  # 门禁未满足不是异常，是判定结果


def test_build_pipeline_failure_records_error_and_failed():
    svc = _make_service(FakePipeline(fail_at="load"))
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    svc.build(inst["id"])
    final = _wait_running_done(svc, inst["id"])
    assert final["status"] == "FAILED"
    assert "装载失败注入" in final["errorMsg"]
    assert final["transitionLog"][-1]["to_state"] == "FAILED"


def test_build_requires_planned_instance():
    svc = _make_service(FakePipeline())
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    svc._repo.update_instance_status(int(inst["id"]), "LOADING")
    with pytest.raises(DatasetServiceError, match="仅适用 PLANNED"):
        svc.build(inst["id"])


def test_resume_requires_partial_and_manifest_id():
    svc = _make_service(FakePipeline())
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    # 人为把实例置为 PARTIAL（模拟上游构建中断）
    svc._repo.update_instance_status(int(inst["id"]), "PARTIAL")
    with pytest.raises(DatasetServiceError, match="manifest_id"):
        svc.build(inst["id"], resume=True)


def test_build_production_environment_rejected():
    svc = _make_service(FakePipeline())
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="production-mysql")
    with pytest.raises(DatasetServiceError, match="生产环境"):
        svc.build(inst["id"])


def test_build_idempotent_while_running():
    svc = _make_service(FakePipeline())
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    first = svc.build(inst["id"])
    second = svc.build(inst["id"])
    assert first["accepted"] is True
    assert second["accepted"] is False  # 执行中不重复排队（任务 ID = instance id）
    _wait_running_done(svc, inst["id"])


# ---------------------------------------------------------------- 服务层：独立验证与清理


def test_verify_only_from_loading():
    svc = _make_service()
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    svc._repo.update_instance_status(int(inst["id"]), "LOADING")
    svc.verify(inst["id"])
    final = _wait_running_done(svc, inst["id"])
    assert final["status"] == "READY"


def test_verify_rejects_ready_instance():
    svc = _make_service()
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    svc._repo.update_instance_status(int(inst["id"]), "READY")
    with pytest.raises(DatasetServiceError, match="仅适用 LOADING"):
        svc.verify(inst["id"])


def test_cleanup_ready_instance_with_approval():
    pipeline = FakePipeline()
    svc = _make_service(pipeline)
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    svc._repo.update_instance_status(int(inst["id"]), "READY")
    result = svc.cleanup(
        inst["id"], env_class="dedicated_test_db",
        approvals=[{"operation": "CLEANUP", "target": "orders",
                    "authorized_by": "op", "ticket": "T-1"}])
    assert result["tier"] == "database_reset"
    assert result["audit"]  # 三级审计快照
    assert svc.get_instance(inst["id"])["status"] == "CLEANED"
    assert ("cleanup", "database_reset") in pipeline.calls


def test_cleanup_blocked_shared_without_owner_decision():
    svc = _make_service()
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    # 共享环境 + 无 owner_token 不可能（创建即发 token）——模拟共享表侧标记缺失
    svc._repo.set_owner_token(int(inst["id"]), None)
    svc._repo.update_instance_status(int(inst["id"]), "READY")
    from ptde.lifecycle.cleanup import CleanupBlocked

    with pytest.raises(CleanupBlocked, match="所有权标记"):
        svc.cleanup(inst["id"], env_class="shared_test")


def test_cleanup_production_env_blocked():
    svc = _make_service()
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="prod-mysql")
    svc._repo.update_instance_status(int(inst["id"]), "READY")
    from ptde.lifecycle.cleanup import CleanupBlocked

    with pytest.raises(CleanupBlocked, match="禁止清理"):
        svc.cleanup(inst["id"], env_class="production")


def test_cleanup_requires_ready_or_failed():
    svc = _make_service()
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    with pytest.raises(DatasetServiceError, match="仅适用 READY/FAILED"):
        svc.cleanup(inst["id"], env_class="dedicated_test_db")


# ---------------------------------------------------------------- API 端点


def test_api_register_and_get_definition():
    client = _make_client()
    resp = client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "订单", "spec": MINIMAL_SPEC})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["specFingerprint"]
    detail = client.get("/api/datasets/plan", params={"defKey": "orders", "version": "1"}).json()
    assert detail["data"]["name"] == "订单"


def test_api_register_duplicate_returns_code_1():
    client = _make_client()
    payload = {"defKey": "orders", "version": "1", "name": "订单", "spec": MINIMAL_SPEC}
    client.post("/api/datasets/plan", json=payload)
    resp = client.post("/api/datasets/plan", json=payload).json()
    assert resp["code"] == 1
    assert "已存在" in resp["message"]


def test_api_create_instance_and_progress():
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    resp = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"}).json()
    assert resp["code"] == 0
    iid = resp["data"]["id"]
    progress = client.get(f"/api/datasets/{iid}/progress").json()
    assert progress["data"]["instanceStatus"] == "PLANNED"
    assert progress["data"]["running"] is False


def test_api_build_async_reaches_ready():
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"}).json()["data"]["id"]
    resp = client.post(f"/api/datasets/{iid}/build",
                       params={"dryRun": False, "seed": 7}).json()
    assert resp["code"] == 0 and resp["data"]["accepted"] is True
    deadline = time.monotonic() + 5
    data = None
    while time.monotonic() < deadline:
        data = client.get(f"/api/datasets/{iid}/progress").json()["data"]
        if not data["running"]:
            break
        time.sleep(0.02)
    assert data["instanceStatus"] == "READY"
    assert data["validationSummary"]["ready"] is True
    # 反查验证报告齐全
    detail = client.get(f"/api/datasets/{iid}", params={"withReports": True}).json()["data"]
    assert len(detail["reports"]) == 5
    # 反查执行清单（§12.4 可复现要素全链可溯）
    manifest = client.get(f"/api/datasets/{iid}/manifest").json()
    assert manifest["code"] == 0
    assert manifest["data"]["instanceId"] == iid
    assert manifest["data"]["seed"] == 42
    assert manifest["data"]["fingerprints"]["plan"] == "p-fp"


def test_api_manifest_unknown_instance_returns_code_1():
    client = _make_client()
    resp = client.get("/api/datasets/999/manifest").json()
    assert resp["code"] == 1
    assert "不存在" in resp["message"]


def test_api_build_production_rejected_with_code_1():
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "production"}).json()["data"]["id"]
    resp = client.post(f"/api/datasets/{iid}/build").json()
    assert resp["code"] == 1
    assert "生产环境" in resp["message"]


def test_api_cleanup_endpoint_flow():
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"}).json()["data"]["id"]
    client.request("POST", f"/api/datasets/{iid}/build")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        data = client.get(f"/api/datasets/{iid}/progress").json()["data"]
        if not data["running"]:
            break
        time.sleep(0.02)
    resp = client.post(f"/api/datasets/{iid}/cleanup", json={
        "envClass": "dedicated_test_db",
        "approvals": [{"operation": "CLEANUP", "target": "orders",
                       "authorizedBy": "op", "ticket": "T-1"}],
    }).json()
    assert resp["code"] == 0
    assert resp["data"]["tier"] == "database_reset"
    assert client.get(f"/api/datasets/{iid}").json()["data"]["status"] == "CLEANED"


def test_api_cleanup_requires_env_class():
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"}).json()["data"]["id"]
    resp = client.post(f"/api/datasets/{iid}/cleanup", json={}).json()
    assert resp["code"] == 1
    assert "envClass" in resp["message"]


def test_api_list_instances_filter_by_status():
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"})
    page = client.get("/api/datasets", params={"status": "PLANNED"}).json()["data"]
    assert len(page["items"]) == 1
    assert page["items"][0]["definitionId"] == "orders@1"  # def_key@version 形态（B6 §4 决策 10）
    assert client.get(
        "/api/datasets", params={"status": "READY"}).json()["data"]["items"] == []


def test_api_instances_cursor_pagination_roundtrip():
    """游标分页：size 翻页 → nextCursor 串接 → 全量收集不重不漏、id 降序、末页收尾。"""
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    created = {
        client.post("/api/datasets", json={
            "defKey": "orders", "version": "1", "environmentId": "dev-1"}
        ).json()["data"]["id"]
        for _ in range(5)
    }
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"size": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/api/datasets", params=params).json()["data"]
        assert page["hasMore"] is (pages < 2)  # 5 行 size=2 → 前两页有更多
        seen.extend(it["id"] for it in page["items"])
        pages += 1
        if not page["hasMore"]:
            assert page["nextCursor"] is None
            break
        assert page["nextCursor"]
        cursor = page["nextCursor"]
    assert pages == 3
    assert sorted(seen) == sorted(created)
    assert len(set(seen)) == len(seen)  # 不重不漏（keyset 游标无漂移）
    ids_int = [int(i) for i in seen]
    assert ids_int == sorted(ids_int, reverse=True)  # id 降序（最新在前）


def test_api_instances_cursor_invalid_rejected():
    """非法游标显式拒绝（code=1），不当首页静默吞掉。"""
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"})
    resp = client.get("/api/datasets", params={"size": 2, "cursor": "not-a-cursor!!"}).json()
    assert resp["code"] == 1
    assert "cursor" in resp["message"]


def test_api_instances_cursor_combined_with_status_filter():
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    for _ in range(3):
        client.post("/api/datasets", json={
            "defKey": "orders", "version": "1", "environmentId": "dev-1"})
    page = client.get("/api/datasets",
                      params={"status": "PLANNED", "size": 2}).json()["data"]
    assert len(page["items"]) == 2
    assert page["hasMore"] is True
    page2 = client.get("/api/datasets", params={
        "status": "PLANNED", "size": 2, "cursor": page["nextCursor"]}).json()["data"]
    assert len(page2["items"]) == 1
    assert page2["hasMore"] is False
    assert all(it["status"] == "PLANNED" for it in page["items"] + page2["items"])


def test_api_instances_limit_fallback_compat():
    """limit 兼容回退（deprecated）：未传 size 时等价 size，仍返回 CursorPage 形态。"""
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    for _ in range(3):
        client.post("/api/datasets", json={
            "defKey": "orders", "version": "1", "environmentId": "dev-1"})
    page = client.get("/api/datasets", params={"limit": 2}).json()["data"]
    assert len(page["items"]) == 2
    assert page["hasMore"] is True


def test_api_unknown_instance_returns_code_1():
    client = _make_client()
    resp = client.get("/api/datasets/999").json()
    assert resp["code"] == 1
    assert "不存在" in resp["message"]


def test_api_tasks_running_flag_reflects_executor():
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"}).json()["data"]["id"]
    client.post(f"/api/datasets/{iid}/build").json()
    task = client.get(f"/api/datasets/{iid}/task").json()["data"]
    assert task["taskId"] == iid
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = client.get(f"/api/datasets/{iid}/task").json()["data"]
        if not task["running"]:
            break
        time.sleep(0.02)
    assert task["running"] is False


# ---------------------------------------------------------------- 取消（B7 续批次 3）


class CancelAwarePipeline(FakePipeline):
    """generate 段消费取消信号的执行体替身（协作式退出协议验证）。"""

    def __init__(self):
        super().__init__()
        self.saw_event = False

    def generate(self, ctx):
        event = ctx.get("cancel_event")
        self.calls.append(("generate", ctx["instance"]["id"]))
        assert event is not None, "ctx 须携带取消事件（信号传递协议）"
        self.saw_event = True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if event.is_set():
                raise DatasetCancelled("生成段收到取消信号（协作式退出）")
            time.sleep(0.01)
        return {"generated_rows": 100}


def _wait_pipeline_call(pipeline, stage: str, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(c[0] == stage for c in pipeline.calls):
            return
        time.sleep(0.01)
    raise AssertionError(f"执行体未进入 {stage} 段")


def test_cancel_planned_instance_direct_transition():
    svc = _make_service(FakePipeline())
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    result = svc.cancel(inst["id"], operator="op-1", reason="不再需要")
    assert result["accepted"] is True
    assert result["state"] == "CANCELLED"
    detail = svc.get_instance(inst["id"])
    assert detail["status"] == "CANCELLED"
    # 审计：PLANNED→CANCELLED 经状态机唯一入口，转换记录落库
    assert ("PLANNED", "CANCELLED") in [
        (t["from_state"], t["to_state"]) for t in detail["transitionLog"]]


def test_cancel_transition_log_records_operator_and_reason():
    svc = _make_service(FakePipeline())
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    svc.cancel(inst["id"], operator="op-1", reason="误建实例")
    record = svc.get_instance(inst["id"])["transitionLog"][-1]
    assert record["operator"] == "op-1"
    assert record["reason"] == "误建实例"
    assert record["from_state"] == "PLANNED"
    assert record["to_state"] == "CANCELLED"


def test_cancel_running_build_signals_pipeline_and_exits_cooperatively():
    pipeline = CancelAwarePipeline()
    svc = _make_service(pipeline)
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    svc.build(inst["id"])
    _wait_pipeline_call(pipeline, "generate")
    result = svc.cancel(inst["id"], operator="op", reason="误建")
    assert result["accepted"] is True
    final = _wait_running_done(svc, inst["id"])
    assert final["status"] == "CANCELLED"
    # 取消信号触达 generate 段（协作式取消协议），后续段不再执行
    assert pipeline.saw_event is True
    assert "load" not in [c[0] for c in pipeline.calls]
    assert ("GENERATING", "CANCELLED") in [
        (t["from_state"], t["to_state"]) for t in final["transitionLog"]]
    assert final["transitionLog"][-1]["to_state"] == "CANCELLED"
    assert final["errorMsg"] is None  # 协作退出不落失败痕迹（取消 ≠ 失败）


def test_cancel_idempotent_no_second_transition():
    svc = _make_service(FakePipeline())
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    first = svc.cancel(inst["id"], operator="op", reason="取消一次")
    second = svc.cancel(inst["id"], operator="op", reason="重复取消")
    assert first["accepted"] is True
    assert second["accepted"] is False  # 幂等：不二次流转
    assert second["state"] == "CANCELLED"
    assert len(svc.get_instance(inst["id"])["transitionLog"]) == 1


def test_cancel_rejects_ready_and_failed():
    svc = _make_service(FakePipeline())
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    svc._repo.update_instance_status(int(inst["id"]), "READY")
    with pytest.raises(DatasetServiceError, match="不可取消"):
        svc.cancel(inst["id"], operator="op", reason="r")
    svc._repo.update_instance_status(int(inst["id"]), "FAILED")
    with pytest.raises(DatasetServiceError, match="不可取消"):
        svc.cancel(inst["id"], operator="op", reason="r")


def test_cancel_rejects_verifying_and_partial_with_guidance():
    svc = _make_service(FakePipeline())
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    svc._repo.update_instance_status(int(inst["id"]), "VERIFYING")
    with pytest.raises(DatasetServiceError, match="VERIFYING"):
        svc.cancel(inst["id"], operator="op", reason="r")
    svc._repo.update_instance_status(int(inst["id"]), "PARTIAL")
    with pytest.raises(DatasetServiceError, match="PARTIAL"):
        svc.cancel(inst["id"], operator="op", reason="r")


def test_cancel_requires_operator_and_reason():
    svc = _make_service(FakePipeline())
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    with pytest.raises(DatasetServiceError, match="operator"):
        svc.cancel(inst["id"], operator="", reason="r")
    with pytest.raises(DatasetServiceError, match="reason"):
        svc.cancel(inst["id"], operator="op", reason="")


def test_cancel_loading_state_allowed():
    """LOADING→CANCELLED 出边评审核实：自 R0 即在（§3.5 三方一致），可取消。"""
    svc = _make_service(FakePipeline())
    svc.register_definition("orders", "1", "n", MINIMAL_SPEC)
    inst = svc.create_instance("orders", "1", environment_id="dev-1")
    svc._repo.update_instance_status(int(inst["id"]), "LOADING")
    result = svc.cancel(inst["id"], operator="op", reason="装载中止")
    assert result["accepted"] is True
    detail = svc.get_instance(inst["id"])
    assert ("LOADING", "CANCELLED") in [
        (t["from_state"], t["to_state"]) for t in detail["transitionLog"]]


def test_api_cancel_endpoint_running_build_flow():
    pipeline = CancelAwarePipeline()
    client = _make_client(pipeline)
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"}).json()["data"]["id"]
    client.post(f"/api/datasets/{iid}/build").json()
    _wait_pipeline_call(pipeline, "generate")
    resp = client.post(f"/api/datasets/{iid}/cancel",
                       json={"operator": "op", "reason": "误建"}).json()
    assert resp["code"] == 0
    assert resp["data"]["accepted"] is True
    deadline = time.monotonic() + 5
    data = None
    while time.monotonic() < deadline:
        data = client.get(f"/api/datasets/{iid}/progress").json()["data"]
        if not data["running"]:
            break
        time.sleep(0.02)
    assert data["instanceStatus"] == "CANCELLED"
    assert data["transitionLog"][-1]["to_state"] == "CANCELLED"


def test_api_cancel_requires_operator_and_reason():
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"}).json()["data"]["id"]
    resp = client.post(f"/api/datasets/{iid}/cancel", json={}).json()
    assert resp["code"] == 1
    assert "operator" in resp["message"]
    resp = client.post(f"/api/datasets/{iid}/cancel",
                       json={"operator": "op"}).json()
    assert resp["code"] == 1
    assert "reason" in resp["message"]


def test_api_cancel_rejects_terminal_state():
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"}).json()["data"]["id"]
    client.post(f"/api/datasets/{iid}/build").json()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        data = client.get(f"/api/datasets/{iid}/progress").json()["data"]
        if not data["running"]:
            break
        time.sleep(0.02)
    assert data["instanceStatus"] == "READY"
    resp = client.post(f"/api/datasets/{iid}/cancel",
                       json={"operator": "op", "reason": "r"}).json()
    assert resp["code"] == 1
    assert "不可取消" in resp["message"]


def test_api_cancel_idempotent_accepted_false():
    client = _make_client()
    client.post("/api/datasets/plan", json={
        "defKey": "orders", "version": "1", "name": "n", "spec": MINIMAL_SPEC})
    iid = client.post("/api/datasets", json={
        "defKey": "orders", "version": "1", "environmentId": "dev-1"}).json()["data"]["id"]
    first = client.post(f"/api/datasets/{iid}/cancel",
                        json={"operator": "op", "reason": "r"}).json()
    second = client.post(f"/api/datasets/{iid}/cancel",
                         json={"operator": "op", "reason": "r"}).json()
    assert first["data"]["accepted"] is True
    assert second["code"] == 0
    assert second["data"]["accepted"] is False
    assert second["data"]["state"] == "CANCELLED"
