"""P0 收尾测试：重放快照（P0-4）/ PTDE 真库接线（P0-2）/ jvm-alert-rules、
schedules、reports/export、deploy（P0-1 剩余四组）。
"""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import server.deploy_repo  # noqa: F401  模型注册（create_all 前）
import server.schedule_service  # noqa: F401
from server.app import create_app
from server.collector_repo import (
    CollectorRepository,
    JvmMetricModel,
    SourceRepoModel,
)
from server.db import Base, make_engine
from server.deploy_repo import DeployConnModel, DeployConnRepository
from server.deploy_service import DeployError, DeployService, ParamikoRunner
from server.issue_verify_service import IssueVerifyService
from server.report_export import safe_filename
from server.schedule_service import (
    StressScheduleRepository,
    StressScheduleService,
)
from server.source_repo import SourceRepository
from server.stress_repo import StressRepository, StressTaskModel


class _StubRunner:
    """create_and_run 提交缝替身（不触发 gevent 闸门）。"""

    def __init__(self):
        self.submitted: list[tuple[int, dict]] = []

    def submit(self, task_id, payload):
        self.submitted.append((task_id, payload))

DT = datetime(2026, 11, 20, 10, 0, 0)


def _factory() -> sessionmaker:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"),
                           expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    return factory


class FakeRunner:
    """SSH 执行体测试替身：记录命令与上传，echo/curl/pgrep/tail 脚本化。"""

    def __init__(self):
        self.commands: list[str] = []
        self.uploads: list[tuple[str, str]] = []

    def exec(self, conn, command, timeout=30):
        self.commands.append(command)
        if "echo $!" in command:
            return 0, "4321"
        if "curl" in command:
            return 0, "200"
        if "pgrep" in command:
            return 0, "4321"
        if "tail" in command:
            return 0, "INFO started"
        if "echo" in command:
            return 0, "ok"
        return 0, ""

    def upload(self, conn, local_path, remote_path):
        self.uploads.append((local_path, remote_path))


class FakeStressService:
    """schedule 触发缝替身：记录 create_and_run 调用。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.repo = None

    def create_and_run(self, payload: dict) -> dict:
        self.calls.append(payload)
        return {"id": len(self.calls), "taskName": "t", "status": "pending"}


# ---------------------------------------------------------------- P0-2 真库接线

def test_dataset_pipeline_factory_assembles_realdb_pipeline(tmp_path):
    """工厂按实例 definition 实探字段/关系装配 OfflinePipeline（P0-2）。"""
    from server.dataset_pipeline_factory import (
        DatasetPipelineWireError,
        realdb_pipeline_factory,
    )
    from server.dataset_service import DatasetLifecycleService
    from server.ptde_repo import PtdeDatasetRepository

    factory = _factory()
    repo = PtdeDatasetRepository(factory)
    svc = DatasetLifecycleService(repo)
    svc.register_definition(
        "orders", "1", "订单数据集",
        {"name": "o", "targets": {"rows": {"t_order": 10}}})
    definition = repo.find_definition("orders", "1")
    fields = {"t_order": [{"name": "id", "type_name": "BIGINT"}]}
    introspected = []

    def fake_introspector(dsn, entities):
        introspected.append((dsn, tuple(entities)))
        return fields, [], {"t_order": "id"}

    build = realdb_pipeline_factory(repo, dsn="mysql://u:p@db.host:3306/perf",
                                    introspector=fake_introspector)
    pipeline = build({"instance": {"id": "1", "definition_id": definition.id}})
    assert type(pipeline).__name__ == "OfflinePipeline"
    assert introspected == [("mysql://u:p@db.host:3306/perf", ("t_order",))]
    # definition 缺失 → 显式失败（不伪装可用）
    with pytest.raises(DatasetPipelineWireError):
        build({"instance": {"id": "1", "definition_id": 99999}})


def test_dataset_service_pipeline_factory_priority(monkeypatch):
    """动态工厂优先于静态注入；两者皆缺省回退 UnwiredPipeline。"""
    from server.dataset_service import DatasetLifecycleService, UnwiredPipeline
    from server.ptde_repo import PtdeDatasetRepository

    repo = PtdeDatasetRepository(_factory())
    sentinel = object()
    svc = DatasetLifecycleService(repo, pipeline_factory=lambda ctx: sentinel)
    assert svc._pipeline_for({"instance": {}}) is sentinel
    svc2 = DatasetLifecycleService(repo)
    assert isinstance(svc2._pipeline_for({}), UnwiredPipeline)


def test_resolve_target_dsn_env_and_jdbc_fallback(monkeypatch):
    """DSN 优先级：显式 → PTDE_REALDB_URL → db_conn 默认连接（jdbc 转换）。"""
    from server.collector_repo import DbConnModel
    from server.dataset_pipeline_factory import (
        DatasetPipelineWireError,
        jdbc_url_to_pymysql,
        resolve_target_dsn,
    )
    from server.ptde_repo import PtdeDatasetRepository

    assert jdbc_url_to_pymysql(
        "jdbc:mysql://h:3306/db?x=1") == "mysql+pymysql://h:3306/db?x=1"
    factory = _factory()
    repo = PtdeDatasetRepository(factory)
    with factory() as s:
        s.add(DbConnModel(conn_name="c1", host="h", db_url="jdbc:mysql://h:3306/db",
                          username="u", password="p", is_default=1))
        s.commit()
    monkeypatch.delenv("PTDE_REALDB_URL", raising=False)
    # P0 修复：db_conn 凭据（含解密）随 DSN 透传（URL 编码防特殊字符破坏解析）
    assert resolve_target_dsn(repo) == "mysql+pymysql://u:p@h:3306/db"
    monkeypatch.setenv("PTDE_REALDB_URL", "mysql://env:u@env-host/db")
    assert resolve_target_dsn(repo) == "mysql://env:u@env-host/db"
    assert resolve_target_dsn(repo, "mysql://explicit/db") == "mysql://explicit/db"
    # 无任何配置 → 显式报错
    monkeypatch.delenv("PTDE_REALDB_URL", raising=False)
    empty_repo = PtdeDatasetRepository(_factory())
    with pytest.raises(DatasetPipelineWireError):
        resolve_target_dsn(empty_repo)


def test_resolve_target_dsn_db_conn_encrypted_credentials(monkeypatch):
    """P0 修复：清空 PTDE_REALDB_URL，仅凭 db_conn（加密特殊字符密码）→
    resolve_target_dsn 返回携带 URL 编码用户名与解密密码的 DSN。"""
    from server.collector_repo import DbConnModel
    from server.crypto import encrypt
    from server.dataset_pipeline_factory import resolve_target_dsn
    from server.ptde_repo import PtdeDatasetRepository

    monkeypatch.delenv("PTDE_REALDB_URL", raising=False)
    monkeypatch.setenv("PERF_CRYPTO_SECRET", "test-secret")
    factory = _factory()
    repo = PtdeDatasetRepository(factory)
    with factory() as s:
        s.add(DbConnModel(conn_name="c1", host="h",
                          db_url="jdbc:mysql://127.0.0.1:3306/demo",
                          username="app user", password=encrypt("p@ss/123"),
                          is_default=1))
        s.commit()
    dsn = resolve_target_dsn(repo)
    assert dsn == "mysql+pymysql://app%20user:p%40ss%2F123@127.0.0.1:3306/demo"
    assert "p@ss/123" not in dsn  # 特殊字符已 URL 编码，DSN 解析不被破坏


# ---------------------------------------------------------------- P0-4 重放快照

def _seed_task(factory, request_payload: dict) -> int:
    with factory() as s:
        task = StressTaskModel(
            task_name="带场景压测", target_url="http://demo/api", app_name="demo",
            status="finished", create_time=DT,
            request_json=json.dumps(request_payload, ensure_ascii=False),
            result_json=json.dumps({"qps": 100, "p95Ms": 200}))
        s.add(task)
        s.commit()
        return task.id


def test_replay_uses_full_request_snapshot():
    """_to_request 优先还原 request_json 完整快照（body/headers/warmup 保留）。"""
    factory = _factory()
    payload = {"targetUrl": "http://demo/api", "method": "POST",
               "body": '{"k":"v"}', "headers": {"Authorization": "Bearer x"},
               "warmupSec": 10, "concurrency": 8, "durationSec": 30}
    task_id = _seed_task(factory, payload)
    snapshot = StressRepository(factory).find_by_id(task_id)
    replay = IssueVerifyService._to_request(snapshot)
    assert replay["body"] == '{"k":"v"}' and replay["headers"]["Authorization"]
    assert replay["warmupSec"] == 10 and replay["concurrency"] == 8
    assert replay["taskName"].startswith("修复验证重放 #")


def test_replay_falls_back_to_field_rebuild():
    """request_json 缺失/坏 JSON → 回退字段重建（旧任务兼容）。"""
    factory = _factory()
    with factory() as s:
        task = StressTaskModel(task_name="旧任务", target_url="http://demo",
                               method="GET", concurrency=4, duration_sec=60,
                               request_json="{bad json", status="finished",
                               create_time=DT)
        s.add(task)
        s.commit()
        task_id = task.id
    snapshot = StressRepository(factory).find_by_id(task_id)
    replay = IssueVerifyService._to_request(snapshot)
    assert replay["targetUrl"] == "http://demo" and replay["concurrency"] == 4
    assert replay["durationSec"] == 60


def test_create_task_persists_request_json(monkeypatch):
    """POST /api/stress/tasks 落库 request_json（完整 payload 快照）。"""
    from server import stress_service as ss_mod

    monkeypatch.setattr(ss_mod, "engine_available", lambda engine_type: True)
    factory = _factory()
    client = TestClient(create_app(stress_repo=StressRepository(factory),
                                   stress_runner=_StubRunner(),
                                   auth_enabled=False))
    data = client.post("/api/stress/tasks", json={
        "targetUrl": "http://demo/", "concurrency": 2,
        "durationSec": 5}).json()["data"]
    row = StressRepository(factory).find_by_id(data["id"])
    assert json.loads(row.request_json)["targetUrl"] == "http://demo/"


# ---------------------------------------------------------------- P0-1a 告警规则

@pytest.fixture()
def alert_ctx():
    factory = _factory()
    client = TestClient(create_app(stress_repo=StressRepository(factory),
                                   collector_repo=CollectorRepository(factory),
                                   auth_enabled=False))
    return client, factory


def _seed_jvm(factory, heap_used, heap_max, full_gc=1):
    from datetime import datetime
    with factory() as s:
        s.add(JvmMetricModel(app_name="demo", collect_time=datetime.now(),
                             heap_used_mb=heap_used, heap_max_mb=heap_max,
                             full_gc_count=full_gc, thread_count=42))
        s.commit()


def test_alert_rule_crud_and_check(alert_ctx):
    client, factory = alert_ctx
    # 新增 + 列表（camel 字段）
    add = client.post("/api/jvm-alert-rules", json={
        "appName": "demo", "metricKey": "heapUsedRatio", "threshold": 80,
        "windowSec": 300, "quietSec": 60, "enabled": 1}).json()
    assert add["code"] == 0
    rule = add["data"]
    assert {"id", "appName", "metricKey", "threshold", "windowSec",
            "quietSec", "enabled", "webhook"} <= set(rule)
    # 重复 key → uk_app_metric 冲突
    dup = client.post("/api/jvm-alert-rules", json={
        "appName": "demo", "metricKey": "heapUsedRatio", "threshold": 90}).json()
    assert dup["code"] == 1
    # 未超阈值：无数据不触发
    r0 = client.post(f"/api/jvm-alert-rules/{rule['id']}/check").json()
    assert r0["data"]["triggered"] is False
    # 超阈值（900/1024=87.9% ≥ 80）→ triggered + lastAlertTime 落库
    _seed_jvm(factory, heap_used=900, heap_max=1024)
    r1 = client.post(f"/api/jvm-alert-rules/{rule['id']}/check").json()["data"]
    assert r1["triggered"] is True and r1["currentValue"] >= 80
    # 更新 + 启停 + 删除
    assert client.put(f"/api/jvm-alert-rules/{rule['id']}",
                      json={"threshold": 99}).json()["data"]["threshold"] == 99
    assert client.post(f"/api/jvm-alert-rules/{rule['id']}/enabled",
                       params={"enabled": False}).json()["data"]["enabled"] == 0
    assert client.delete(f"/api/jvm-alert-rules/{rule['id']}").json()["code"] == 0
    # 指标清单
    keys = [m["key"] for m in client.get("/api/jvm-alert-rules/metrics").json()["data"]]
    assert keys == ["heapUsedRatio", "fullGcCountDelta", "threadCount"]


# ---------------------------------------------------------------- P0-1b 调度

def test_schedule_crud_toggle_and_diff():
    factory = _factory()
    stress_repo = StressRepository(factory)
    fake = FakeStressService()
    fake.repo = stress_repo
    svc = StressScheduleService(StressScheduleRepository(factory), fake)
    row = svc.create({"scheduleName": "回归压测", "intervalMinutes": 60,
                      "baselineTaskId": 0,
                      "task": {"targetUrl": "http://demo/", "concurrency": 4}})
    assert row.to_camel()["nextRunTime"] is not None
    # 未到期不触发
    assert svc.run_due() == [] and fake.calls == []
    # 到期触发：先推进节拍再发起（防重复），模板原样透传
    from datetime import datetime, timedelta
    svc.repo.update_trigger(row.id, last_task_id=0, last_run_time=datetime.now(),
                            next_run_time=datetime.now() - timedelta(minutes=1))
    fired = svc.run_due()
    assert [f["taskId"] for f in fired] == [1]
    assert fake.calls[0]["targetUrl"] == "http://demo/"
    updated = svc.repo.find_by_id(row.id)
    assert updated.last_task_id == 1 and updated.next_run_time > datetime.now()
    # 重复 run_due 不二次触发（节拍已推进）
    assert svc.run_due() == []


def test_schedule_diff_no_task_yet():
    factory = _factory()
    svc = StressScheduleService(StressScheduleRepository(factory), FakeStressService())
    row = svc.create({"scheduleName": "s", "intervalMinutes": 5,
                      "task": {"targetUrl": "http://x/"}})
    assert svc.diff(row.id)["noBaseline"] is True


# ---------------------------------------------------------------- P0-1c 报告导出

@pytest.fixture()
def report_ctx():
    factory = _factory()
    with factory() as s:
        s.add(StressTaskModel(task_name="导出/测试:任务", target_url="http://demo/",
                              status="finished", create_time=DT,
                              result_json=json.dumps({
                                  "qps": 320, "p95Ms": 210,
                                  "stagePoints": [{"concurrency": 10, "qps": 180}],
                                  "capacity": {"maxQps": 420, "maxConcurrency": 40,
                                               "advices": [{"title": "扩容",
                                                            "scaleType": "水平扩容",
                                                            "riskLevel": "medium",
                                                            "description": "d"}]}})))
        s.commit()
    client = TestClient(create_app(stress_repo=StressRepository(factory),
                                   collector_repo=CollectorRepository(factory),
                                   auth_enabled=False))
    return client


def test_report_export_html_and_doc(report_ctx):
    client = report_ctx
    url = "/api/reports/export?stressTaskId=1&format=html"
    resp = client.get(url)
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.text
    assert "压测报告" in body and "容量评估" in body and "扩容" in body
    assert safe_filename({"id": 1, "taskName": "导出/测试:任务"}, "html") == \
        "report-1-导出_测试_任务.html"
    doc = client.get("/api/reports/export?stressTaskId=1&format=doc")
    assert doc.headers["content-type"].startswith("application/msword")
    # printMode → inline 打印视图（自动唤起打印）
    inline = client.get("/api/reports/export?stressTaskId=1&printMode=true")
    assert "inline" in inline.headers["content-disposition"]
    assert "window.print" in inline.text
    assert client.get("/api/reports/export?stressTaskId=999").json()["code"] == 1


# ---------------------------------------------------------------- P0-1d deploy

def _deploy_ctx(factory, runner):
    with factory() as s:
        s.add(SourceRepoModel(repo_name="demo", source_path="/tmp/demo-src"))
        s.add(DeployConnModel(conn_name="edge-1", host="10.0.0.8", port=22,
                              username="root", auth_type="password",
                              password="pwd", work_dir="/opt/p"))
        s.commit()
    client = TestClient(create_app(
        stress_repo=StressRepository(factory),
        collector_repo=CollectorRepository(factory),
        deploy_conn_repo=DeployConnRepository(factory),
        deploy_service=DeployService(DeployConnRepository(factory),
                                     SourceRepository(factory),
                                     runner=runner),
        auth_enabled=False))
    return client


def test_deploy_conns_wire_and_crud():
    factory = _factory()
    client = _deploy_ctx(factory, FakeRunner())
    data = client.get("/api/deploy/conns").json()["data"]
    assert data[0]["connName"] == "edge-1" and data[0]["host"] == "10.0.0.8"
    assert "password" not in data[0] and "privateKey" not in data[0]  # 凭据不出网
    added = client.post("/api/deploy/conns", json={
        "connName": "edge-2", "host": "10.0.0.9", "username": "root"}).json()
    assert added["code"] == 0 and added["data"]["port"] == 22
    assert client.delete("/api/deploy/conns/1").json()["code"] == 0


def test_deploy_test_start_status_with_fake_runner():
    factory = _factory()
    runner = FakeRunner()
    client = _deploy_ctx(factory, runner)
    # 探活（按 id，凭据不出服务端）
    probe = client.post("/api/deploy/conns/1/test").json()
    assert probe["code"] == 0 and probe["data"]["reachable"] is True
    # 远程启动：mkdir + 上传 jar/agent + nohup java -javaagent
    jar = "/tmp/demo.jar"
    open(jar, "wb").write(b"fake-jar")
    start = client.post("/api/deploy/start", json={
        "connId": 1, "jarPath": jar, "appPort": 8081}).json()["data"]
    assert start["pid"] == "4321" and start["appPort"] == 8081
    assert any(u[1] == "/opt/p/app.jar" for u in runner.uploads)
    assert any("javaagent" in c and "appPort=8081" in c for c in runner.commands)
    # 状态：进程 + 健康 + 日志尾部
    st = client.get("/api/deploy/status", params={"connId": 1}).json()["data"]
    assert st["running"] is True and st["pid"] == "4321" and "started" in st["logTail"]
    # jar 缺失 → 业务错误包装
    missing = client.post("/api/deploy/start", json={
        "connId": 1, "jarPath": "/tmp/nope.jar"}).json()
    assert missing["code"] == 1 and "jar 不存在" in missing["message"]


def test_deploy_missing_paramiko_message():
    """ParamikoRunner 懒加载缺依赖时显式报错（不伪装可用）。"""
    factory = _factory()
    service = DeployService(DeployConnRepository(factory), None,
                            runner=ParamikoRunner())
    with factory() as s:
        s.add(DeployConnModel(conn_name="c", host="10.0.0.1", username="root"))
        s.commit()
    try:
        service.test_conn_by_id(1)
        pytest.skip("paramiko 已安装，跳过缺依赖分支")
    except DeployError as exc:
        assert "perf-py[deploy]" in str(exc)


def test_deploy_conn_credentials_encrypted_at_rest(monkeypatch):
    """P0 修复回归：POST /api/deploy/conns 落库后 password/private_key 非明文。"""
    from server.crypto import decrypt

    monkeypatch.setenv("PERF_CRYPTO_SECRET", "test-secret")
    factory = _factory()
    client = TestClient(create_app(
        stress_repo=StressRepository(factory),
        collector_repo=CollectorRepository(factory),
        deploy_conn_repo=DeployConnRepository(factory),
        deploy_service=DeployService(DeployConnRepository(factory),
                                     SourceRepository(factory),
                                     runner=FakeRunner()),
        auth_enabled=False))
    resp = client.post("/api/deploy/conns", json={
        "connName": "enc-1", "host": "10.0.0.9", "username": "root",
        "password": "s3cret!", "privateKey": "-----BEGIN PRIVATE KEY-----"}).json()
    assert resp["code"] == 0
    with factory() as s:
        row = s.get(DeployConnModel, resp["data"]["id"])
        assert row.password.startswith("ENC:") and "s3cret!" not in row.password
        assert row.private_key.startswith("ENC:")
        assert decrypt(row.password) == "s3cret!"
        assert decrypt(row.private_key) == "-----BEGIN PRIVATE KEY-----"


def test_deploy_remote_commands_are_shell_quoted(tmp_path):
    """P0 修复回归：work_dir/app_name 含 ; 与空格时全部远端参数单引号转义，无命令注入。"""
    import shlex

    factory = _factory()
    runner = FakeRunner()
    work = "/opt/p; id"
    with factory() as s:
        s.add(DeployConnModel(conn_name="edge-x", host="10.0.0.8", port=22,
                              username="root", auth_type="password",
                              password="pwd", work_dir=work))
        s.commit()
    svc = DeployService(DeployConnRepository(factory), None, runner=runner)
    jar = tmp_path / "demo.jar"
    jar.write_bytes(b"fake-jar")
    out = svc.start(1, str(jar), app_port=8081, app_name="app; rm -rf /")
    assert out["appPort"] == 8081
    assert runner.commands[0] == f"mkdir -p {shlex.quote(work)}"
    test_agent = next(c for c in runner.commands if c.startswith("test -f"))
    assert test_agent == f"test -f {shlex.quote(f'{work}/perf-agent.jar')} && echo ok"
    launch = next(c for c in runner.commands if "nohup java" in c)
    javaagent = shlex.quote(
        f"{work}/perf-agent.jar=reportUrl=http://127.0.0.1:8000/api/agent/report"
        ",appName=app; rm -rf /,appPort=8081")
    assert launch.startswith(f"cd {shlex.quote(work)} && nohup java")
    assert f"-javaagent:{javaagent}" in launch
    assert f"-jar {shlex.quote(f'{work}/app.jar')}" in launch
    assert f"> {shlex.quote(f'{work}/app.log')}" in launch
    # 分号注入串只出现在单引号包裹内（作为字面量），未构成命令分隔
    for c in runner.commands:
        assert c.count("'") % 2 == 0  # 引号成对闭合


def test_deploy_invalid_app_port_rejected(tmp_path):
    """P0 修复回归：app_port 非整型/越界 → DeployError，远端命令不拼接。"""
    factory = _factory()
    runner = FakeRunner()
    with factory() as s:
        s.add(DeployConnModel(conn_name="edge-y", host="10.0.0.8",
                              username="root", work_dir="/opt/p"))
        s.commit()
    svc = DeployService(DeployConnRepository(factory), None, runner=runner)
    jar = tmp_path / "demo.jar"
    jar.write_bytes(b"x")
    for bad_port in (0, 99999, "; id"):
        with pytest.raises(DeployError, match="app_port"):
            svc.start(1, str(jar), app_port=bad_port)
    with pytest.raises(DeployError, match="app_port"):
        svc.status(1, app_port=70000)
    assert runner.commands == []  # 校验失败不产生任何远端命令
