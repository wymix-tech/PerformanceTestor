"""一键验证契约测试（阶段 5 · 步骤 11）：以 Java IssueVerifyService 源码为契约。

覆盖：场景快照解析守卫（文案逐字）、同步判定（达标/未达标/回归三路文案）、
异步验证线程池语义（running 落库 + 重复发起拒绝 + 轮询终态）、重放启动失败落库、
服务重启残留 running 的时间兜底、deadlineMs 预算规则。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.collector_repo import CollectorRepository
from server.db import make_engine
from server.issue_verify_service import IssueVerifyService, deadline_ms
from server.knowledge import SqliteKnowledgeStore
from server.source_repo import PerformanceIssueModel, SourceRepository
from server.source_write_service import DataCleanupService
from server.stress_repo import (
    Base,
    StressRepository,
    StressTaskModel,
)

SNAPSHOT_RESULT = {"qps": 100.0, "p95Ms": 100.0, "p99Ms": 150.0, "errorRate": 0.5}


class FakeStressService:
    """假压测服务：按脚本插入重放任务（finished 或延迟翻转）。"""

    def __init__(self, repo: StressRepository, replay_result: dict,
                 flip_delay: float | None = None):
        self.repo = repo
        self.replay_result = replay_result
        self.flip_delay = flip_delay
        self.requests: list[dict] = []
        self.fail_message: str | None = None

    def create_and_run(self, payload: dict) -> dict:
        self.requests.append(payload)
        if self.fail_message is not None:
            raise RuntimeError(self.fail_message)
        now = datetime.now()
        status = "running" if self.flip_delay is not None else "finished"
        result = "{}" if self.flip_delay is not None else json.dumps(self.replay_result)
        with self.repo._sessions() as s:  # noqa: SLF001
            task = StressTaskModel(
                task_name=payload["taskName"], engine_type=payload["engineType"],
                target_url=payload["targetUrl"], app_name=payload["appName"] or "",
                method=payload["method"], concurrency=payload["concurrency"],
                duration_sec=payload["durationSec"], qps_limit=payload["qpsLimit"],
                status=status, result_json=result, create_time=now,
                finish_time=None if self.flip_delay is not None else now)
            s.add(task)
            s.commit()
            task_id = task.id
        if self.flip_delay is not None:
            def _flip():
                time.sleep(self.flip_delay)
                with self.repo._sessions() as s2:  # noqa: SLF001
                    row = s2.get(StressTaskModel, task_id)
                    row.status = "finished"
                    row.result_json = json.dumps(self.replay_result)
                    row.finish_time = datetime.now()
                    s2.commit()
            threading.Thread(target=_flip, daemon=True).start()
        return {"id": task_id, "taskName": payload["taskName"], "status": "pending",
                "resultJson": None, "createTime": None, "finishTime": None,
                "errorMsg": None}


@pytest.fixture()
def env(tmp_path):
    """内存库 + 注入式验证服务（inline 同步判定）+ 快照/问题种子。"""
    fac = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(fac.kw["bind"])
    stress_repo, source_repo = StressRepository(fac), SourceRepository(fac)
    fake = FakeStressService(stress_repo, {"qps": 120.0, "p95Ms": 50.0, "p99Ms": 80.0,
                                           "errorRate": 0.2})
    app = create_app(
        stress_repo=stress_repo, collector_repo=CollectorRepository(fac),
        source_repo=source_repo,
        source_storage_dir=str(tmp_path / "storage"),
        data_cleanup=DataCleanupService(fac, SqliteKnowledgeStore()),
        issue_verify=IssueVerifyService(source_repo, stress_repo, fake, inline=True),
        auth_enabled=False)
    client = TestClient(app)

    now = datetime(2026, 11, 16, 10, 0, 0)
    with fac() as s:
        snapshot = StressTaskModel(task_name="基线", engine_type="http",
                                   target_url="http://x/api/orders", app_name="demo",
                                   method="GET", concurrency=50, duration_sec=60,
                                   qps_limit=0, status="finished",
                                   result_json=json.dumps(SNAPSHOT_RESULT),
                                   create_time=now, finish_time=now)
        s.add(snapshot)
        s.commit()
        snapshot_id = snapshot.id
    return client, fac, source_repo, snapshot_id, fake


def _seed_issue(fac, snapshot_id: int, status: str = "analyzed",
                issue_type: str = "stress_bottleneck") -> int:
    now = datetime.now()
    with fac() as s:
        issue = PerformanceIssueModel(
            issue_type=issue_type, app_name="demo", title="压测瓶颈",
            evidence_json="{}", status=status, stress_task_id=snapshot_id,
            create_time=now)
        s.add(issue)
        s.commit()
        return issue.id


# ---------------------------------------------------------------- 快照守卫


def test_verify_issue_missing(env):
    client, _, _, _, _ = env
    assert client.post("/api/source/issues/999/verify").json() == {
        "code": 1, "message": "问题不存在: 999", "data": None}


def test_verify_no_snapshot_not_persisted(env):
    """未关联快照：noSnapshot 结论不落库（不污染问题状态与验证时间）。"""
    client, fac, _, _, _ = env
    issue_id = _seed_issue(fac, 0)
    body = client.post(f"/api/source/issues/{issue_id}/verify").json()
    assert body["code"] == 0
    data = body["data"]
    assert data["noSnapshot"] is True and data["running"] is False
    assert data["message"] == ("该问题未关联压测场景快照，无法自动验证；"
                               "可从压测任务创建问题以建立场景快照")
    assert data["status"] == "analyzed", "保持原状态"
    assert client.get(f"/api/source/issues/{issue_id}/verify").json()["data"] is None


def test_verify_snapshot_deleted(env):
    client, fac, _, _, _ = env
    issue_id = _seed_issue(fac, 999)
    data = client.post(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert data["noSnapshot"] is True
    assert data["message"] == "场景快照压测任务 #999 已不存在，无法自动验证"


# ---------------------------------------------------------------- 同步判定三路


def test_verify_passed_resolves_issue(env):
    """达标（P95 降 50% ≥ 30% 且无回归）→ status=resolved + 结论落库。

    POST /verify 恒返回 started 进行中快照（Java verify 同语义），终态经 GET 轮询。
    """
    client, fac, _, snapshot_id, fake = env
    issue_id = _seed_issue(fac, snapshot_id)
    started = client.post(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert started["running"] is True, "HTTP 层恒为进行中快照"
    data = client.get(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert data["passed"] is True and data["replayFailed"] is False
    assert data["p95Before"] == 100.0 and data["p95After"] == 50.0
    assert data["p95DropPct"] == 50.0 and data["qpsAfter"] == 120.0
    assert data["status"] == "resolved"
    assert data["message"] == ("验证通过：P95 由 100ms 降至 50ms（下降 50%），"
                               "无指标回归，问题已自动关闭")
    assert data["snapshotTaskId"] == snapshot_id and data["replayTaskId"] > 0
    detail = client.get(f"/api/source/issues/{issue_id}").json()["data"]
    assert detail["issue"]["status"] == "resolved", "达标自动关闭"
    assert detail["issue"]["verifyTime"] is not None
    # 结论 JSON 落库（rows 复用基线对比表格语义）
    stored = json.loads(detail["issue"]["verifyJson"])
    assert stored["passed"] is True and stored["status"] == "resolved"
    assert stored["criteria"] == "P95 下降 ≥30%，且无指标回归"
    assert [r["label"] for r in stored["rows"]] == ["QPS", "P95 (ms)", "P99 (ms)", "错误率 (%)"]
    assert stored["rows"][0] == {"label": "QPS", "base": "100", "cur": "120",
                                 "deltaPct": "+20.0%", "improved": True, "regressed": False}
    # 重放请求与快照同场景
    req = fake.requests[0]
    assert req["taskName"] == f"修复验证重放 #{snapshot_id}"
    assert req["targetUrl"] == "http://x/api/orders" and req["concurrency"] == 50
    assert req["durationSec"] == 60 and req["method"] == "GET"


def test_verify_not_passed_keeps_status(env):
    """未达标（P95 仅降 10%）：文案逐字对齐，状态保持。"""
    client, fac, source_repo, snapshot_id, fake = env
    issue_id = _seed_issue(fac, snapshot_id)
    fake.replay_result = {"qps": 100.0, "p95Ms": 90.0, "p99Ms": 150.0, "errorRate": 0.5}
    client.post(f"/api/source/issues/{issue_id}/verify")
    data = client.get(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert data["passed"] is False and data["status"] == "analyzed"
    assert data["message"] == ("验证未达标：P95 由 100ms 变为 90ms（下降 10%，需 ≥ 30%）。"
                               "问题保持原状态，建议复查修复是否生效。")


def test_verify_regressed_blocks_pass(env):
    """P95 降 45% 但 QPS 回归 -30%（>10%）→ 不达标，文案含回归明细。"""
    client, fac, _, snapshot_id, fake = env
    issue_id = _seed_issue(fac, snapshot_id)
    fake.replay_result = {"qps": 70.0, "p95Ms": 55.0, "p99Ms": 150.0, "errorRate": 0.5}
    client.post(f"/api/source/issues/{issue_id}/verify")
    data = client.get(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert data["passed"] is False and data["regressed"] is True
    assert data["status"] == "analyzed"
    assert data["message"] == ("验证未达标：P95 由 100ms 变为 55ms（下降 45%，需 ≥ 30%）；"
                               "且存在指标回归：QPS -30.0%。问题保持原状态，"
                               "建议复查修复是否生效。")


def test_verify_stress_type_passes_via_qps_gain(env):
    """Fix-5：压测瓶颈类「或」规则——P95 仅降 10% 但 QPS +20% ≥ 15% → 达标，
    message 走 QPS 文案，criteria 落库说明命中规则。"""
    client, fac, _, snapshot_id, fake = env
    issue_id = _seed_issue(fac, snapshot_id)
    fake.replay_result = {"qps": 120.0, "p95Ms": 90.0, "p99Ms": 150.0, "errorRate": 0.5}
    client.post(f"/api/source/issues/{issue_id}/verify")
    data = client.get(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert data["passed"] is True and data["status"] == "resolved"
    assert data["regressed"] is False
    assert data["message"] == ("验证通过：QPS 由 100 提升至 120"
                               "（提升 20%，命中 QPS ≥15% 验收规则），"
                               "无指标回归，问题已自动关闭")
    stored = json.loads(client.get(f"/api/source/issues/{issue_id}").json()
                        ["data"]["issue"]["verifyJson"])
    assert stored["criteria"] == "QPS 提升 ≥15%，且无指标回归"


def test_verify_non_stress_type_keeps_p95_only_rule(env):
    """Fix-5：非压测类（slow_sql / hot_method）沿用「且」规则——
    P95 仅降 10%，即便 QPS +20% 也不达标，且 verify_json 无 criteria 字段。"""
    client, fac, _, snapshot_id, fake = env
    issue_id = _seed_issue(fac, snapshot_id, issue_type="slow_sql")
    fake.replay_result = {"qps": 120.0, "p95Ms": 90.0, "p99Ms": 150.0, "errorRate": 0.5}
    client.post(f"/api/source/issues/{issue_id}/verify")
    data = client.get(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert data["passed"] is False and data["status"] == "analyzed"
    stored = json.loads(client.get(f"/api/source/issues/{issue_id}").json()
                        ["data"]["issue"]["verifyJson"])
    assert "criteria" not in stored


def test_verify_replay_start_failure_persisted(env):
    """重放启动失败：结论落库（replayFailed），状态保持。"""
    client, fac, _, snapshot_id, fake = env
    issue_id = _seed_issue(fac, snapshot_id)
    fake.fail_message = "引擎不可用"
    data = client.post(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert data["replayFailed"] is True and data["replayTaskId"] is None
    assert data["message"] == "重放压测启动失败：引擎不可用"
    assert data["status"] == "analyzed"
    stored = json.loads(client.get(f"/api/source/issues/{issue_id}").json()
                        ["data"]["issue"]["verifyJson"])
    assert stored["replayFailed"] is True, "失败结论落库"


# ---------------------------------------------------------------- 异步语义


def test_verify_async_running_then_final(tmp_path):
    """inline=False：verify 毫秒级返回 running 结论（落库），随后轮询到终态；
    进行中重复发起被拒绝（文案逐字）。"""
    fac = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(fac.kw["bind"])
    stress_repo, source_repo = StressRepository(fac), SourceRepository(fac)
    now = datetime.now()
    with fac() as s:
        snapshot = StressTaskModel(task_name="基线", engine_type="http",
                                   target_url="http://x/api", app_name="demo", method="GET",
                                   concurrency=10, duration_sec=30, qps_limit=0,
                                   status="finished",
                                   result_json=json.dumps(SNAPSHOT_RESULT),
                                   create_time=now, finish_time=now)
        s.add(snapshot)
        s.commit()
        snapshot_id = snapshot.id
        issue = PerformanceIssueModel(issue_type="stress_bottleneck", app_name="demo",
                                      title="t", evidence_json="{}", status="analyzed",
                                      stress_task_id=snapshot_id, create_time=now)
        s.add(issue)
        s.commit()
        issue_id = issue.id
    # 重放任务延迟 0.3s 翻转为 finished：制造可观察的"进行中"窗口
    fake = FakeStressService(stress_repo, {"qps": 120.0, "p95Ms": 50.0, "p99Ms": 80.0,
                                           "errorRate": 0.2}, flip_delay=0.3)
    svc = IssueVerifyService(source_repo, stress_repo, fake, timeout_sec=10,
                             inline=False)
    app = create_app(
        stress_repo=stress_repo, collector_repo=CollectorRepository(fac),
        source_repo=source_repo, source_storage_dir=str(tmp_path / "s"),
        data_cleanup=DataCleanupService(fac, SqliteKnowledgeStore()),
        issue_verify=svc, auth_enabled=False)
    client = TestClient(app)

    first = client.post(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert first["running"] is True and first["replayTaskId"] > 0
    assert first["message"] == (f"已发起重放压测（任务 #{first['replayTaskId']}），"
                                "完成后自动给出验证结论")

    # 进行中重复发起：拒绝并返回进行中结论
    second = client.post(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert second["running"] is True
    assert second["message"] == "已有一次验证在进行中，请勿重复发起"
    assert second["replayTaskId"] is None, "Java pending(重复发起) 不带 replayTaskId"

    # 轮询到终态：判定落库
    for _ in range(60):
        status = client.get(f"/api/source/issues/{issue_id}/verify").json()["data"]
        if status is not None and status["running"] is False:
            break
        time.sleep(0.1)
    assert status["passed"] is True and status["status"] == "resolved"
    svc.shutdown()


def test_verify_stale_running_detected(env):
    """服务重启残留 running 结论：按 verifyTime + TTL 兜底判中断（文案逐字）。"""
    client, fac, _, _, _ = env
    issue_id = _seed_issue(fac, 0, status="analyzed")
    stale_time = datetime.now() - timedelta(seconds=400)  # TTL = max(30,300)+60 = 360s
    with fac() as s:
        row = s.get(PerformanceIssueModel, issue_id)
        row.verify_json = json.dumps({"running": True, "passed": False,
                                      "replayFailed": False, "message": "进行中"})
        row.verify_time = stale_time
        s.commit()
    data = client.get(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert data["running"] is False and data["replayFailed"] is True
    assert data["message"] == "上次验证已中断（服务重启或验证进程异常），请重新发起验证"


def test_verify_status_parses_fresh_running(env):
    """未过期的 running 结论原样返回（不误判中断）。"""
    client, fac, _, _, _ = env
    issue_id = _seed_issue(fac, 0)
    with fac() as s:
        row = s.get(PerformanceIssueModel, issue_id)
        row.verify_json = json.dumps({"running": True, "message": "进行中"})
        row.verify_time = datetime.now()
        s.commit()
    data = client.get(f"/api/source/issues/{issue_id}/verify").json()["data"]
    assert data["running"] is True and data["message"] == "进行中"


# ------------------------------------------------------------ 等待预算规则（静态单测）


def test_deadline_ms_budget_rules():
    now = 1_000_000
    # 配置 300s、时长 60s → min(300, 60+60) = 120s
    assert deadline_ms(now, 300, 60) == now + 120_000
    # 时长未知 → 按 60s 收尾缓冲兜底 → min(300, 60) = 60s
    assert deadline_ms(now, 300, None) == now + 60_000
    # 配置 10s（低于下限 30s）→ max(30,10)=30；时长 5s+60=65 → min = 30s
    assert deadline_ms(now, 10, 5) == now + 30_000
    # 配置 5s、时长 100s（超配置）→ 以配置侧下限 30s 为准，避免无限等待
    assert deadline_ms(now, 5, 100) == now + 30_000
