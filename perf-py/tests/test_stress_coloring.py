"""P1-1 压测流量染色测试：压测请求注入染色头 → 探针上报携带 stressTaskId →
落库与按 stressTaskId 精确反查（trace / slow_sql / slow_command 三通道）。

覆盖验收标准：
- 引擎统一注入：单接口与多步场景每步均携带 X-Perf-Stress-Task-Id（不覆盖用户显式同名头）；
- JMeter JMX（单接口与 steps 导出模式）同样包含染色头；
- sampleRate=0 场景（旧探针/未采样）不阻断染色：上报无 stressTaskId 时落库为 NULL；
- GET /api/slow-sqls?stressTaskId=... 仅返回该次压测的数据，不混入其他流量。
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from engine.jmeter_engine import generate_jmx
from server.app import create_app
from server.collector_repo import (
    CollectorRepository,
    SlowCommandModel,
    SlowSqlModel,
    SourceRepoModel,
    TraceChainModel,
)
from server.collector_service import _stress_task_id_val
from server.db import make_engine
from server.stress_repo import Base, StressRepository
from server.stress_request import (
    STRESS_TASK_ID_HEADER,
    TRAFFIC_TAG_HEADER,
    parse_stress_request,
    stage_scenario,
    to_scenario,
)

T0 = 1794572783584  # epoch ms


def _request_payload(**over) -> dict:
    payload = {
        "taskName": "coloring",
        "targetUrl": "http://demo:8081/api/order/list",
        "method": "GET",
        "concurrency": 2,
        "durationSec": 3,
        "headers": {"A": "b"},
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------- 场景桥接注入


def test_to_scenario_single_mode_injects_coloring_headers():
    r = parse_stress_request(_request_payload())
    s = to_scenario(r, task_id=7)
    assert s.request.headers[STRESS_TASK_ID_HEADER] == "7"
    assert s.request.headers[TRAFFIC_TAG_HEADER] == "stress"
    assert s.request.headers["A"] == "b"


def test_to_scenario_scenario_mode_injects_headers_into_every_step():
    steps = [{"url": "http://demo/a", "method": "GET"},
             {"url": "http://demo/b", "method": "POST", "body": "{}"}]
    r = parse_stress_request(_request_payload(targetUrl=None, scenario=steps))
    s = to_scenario(r, task_id=9)
    assert [st.headers[STRESS_TASK_ID_HEADER] for st in s.steps] == ["9", "9"]
    assert all(st.headers[TRAFFIC_TAG_HEADER] == "stress" for st in s.steps)


def test_explicit_user_header_not_overwritten():
    r = parse_stress_request(_request_payload(
        headers={"A": "b", STRESS_TASK_ID_HEADER: "user-value"}))
    s = to_scenario(r, task_id=7)
    # 用户显式配置的同名头不被覆盖
    assert s.request.headers[STRESS_TASK_ID_HEADER] == "user-value"


def test_task_id_none_only_adds_traffic_tag():
    r = parse_stress_request(_request_payload())
    s = to_scenario(r)
    assert STRESS_TASK_ID_HEADER not in s.request.headers
    assert s.request.headers[TRAFFIC_TAG_HEADER] == "stress"


def test_stage_scenario_propagates_task_id():
    r = parse_stress_request(_request_payload(
        stages=[{"concurrency": 4, "durationSec": 5}]))
    stage = r.resolve_stages()[0]
    s = stage_scenario(r, stage, with_warmup=True, task_id=11)
    assert s.request.headers[STRESS_TASK_ID_HEADER] == "11"
    # 不传 task_id 保持旧行为
    s0 = stage_scenario(r, stage, with_warmup=True)
    assert STRESS_TASK_ID_HEADER not in s0.request.headers


# ---------------------------------------------------------- 引擎契约


def test_jmeter_jmx_contains_coloring_header():
    r = parse_stress_request(_request_payload())
    jmx = generate_jmx(to_scenario(r, task_id=21))
    assert STRESS_TASK_ID_HEADER in jmx
    assert ">21<" in jmx


def test_jmeter_jmx_steps_mode_contains_coloring_headers():
    steps = [{"url": "http://demo/a", "method": "GET"},
             {"url": "http://demo/b", "method": "GET"}]
    r = parse_stress_request(_request_payload(targetUrl=None, scenario=steps))
    jmx = generate_jmx(to_scenario(r, task_id=22), steps_mode=True)
    assert jmx.count(STRESS_TASK_ID_HEADER) == 2


# ---------------------------------------------------------- run_task 透传


class _EngineStub:
    """捕获引擎收到的 Scenario（替代真引擎，避免 locust 导入闸门）。"""

    def __init__(self):
        self.scenarios = []

    def run(self, scenario, listener=None):  # noqa: ANN001
        self.scenarios.append(scenario)
        from engine.result import StressTaskResult
        return StressTaskResult(
            task_id=0, engine_type="locust", target_url=scenario.request.url,
            concurrency=scenario.load.concurrency, duration_sec=scenario.load.duration_sec,
            warmup_sec=0, ramp_up_sec=0, total_requests=10, error_count=0,
            error_rate=0.0, qps=1.0, p50_ms=1.0, p95_ms=1.0, p99_ms=1.0,
            samples=[], stage_points=[], warnings=[])


def test_run_task_passes_task_id_to_engine_scenario():
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    engine = _EngineStub()
    from server.stress_service import run_task

    run_task(33, _request_payload(), factory, engine=engine)
    assert engine.scenarios[0].request.headers[STRESS_TASK_ID_HEADER] == "33"


# ---------------------------------------------------------- 上报解析与落库


def test_stress_task_id_val_strict_positive_int():
    assert _stress_task_id_val("42") == 42
    assert _stress_task_id_val(42) == 42
    assert _stress_task_id_val(42.0) == 42
    # 非法值一律静默置 None（防伪造脏头落库异常）
    assert _stress_task_id_val("0") is None
    assert _stress_task_id_val("-1") is None
    assert _stress_task_id_val("abc") is None
    assert _stress_task_id_val("12x") is None
    assert _stress_task_id_val(None) is None
    assert _stress_task_id_val(True) is None
    assert _stress_task_id_val(0) is None
    assert _stress_task_id_val(3.5) is None


@pytest.fixture()
def ctx():
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    client = TestClient(create_app(stress_repo=StressRepository(factory),
                                   collector_repo=CollectorRepository(factory),
                                   auth_enabled=False))
    with factory() as s:
        s.add(SourceRepoModel(repo_name="demo-app"))
        s.commit()
    return client, factory


def _post(client, report_type, items):
    body = {"type": report_type, "appName": "demo-app", "items": items}
    resp = client.post("/api/agent/report", json=body)
    assert resp.status_code == 200


def test_report_with_stress_task_id_persists_to_all_three_tables(ctx):
    client, factory = ctx
    _post(client, "slowSql", [{"sqlText": "SELECT 1", "costMs": 900,
                               "executeTime": T0, "stressTaskId": "55"}])
    _post(client, "trace", [{"traceId": "t-1", "rootMethod": "C#m", "totalMs": 12,
                             "startTime": T0, "tree": {}, "stressTaskId": 55}])
    _post(client, "slowCommand", [{"type": "redis", "command": "get k", "costMs": 200,
                                   "executeTime": T0, "stressTaskId": "55"}])
    with factory() as s:
        assert s.query(SlowSqlModel).first().stress_task_id == 55
        assert s.query(TraceChainModel).first().stress_task_id == 55
        assert s.query(SlowCommandModel).first().stress_task_id == 55


def test_report_without_stress_task_id_stays_null(ctx):
    """向后兼容：旧版探针不带染色字段时落库为 NULL，不报错。"""
    client, factory = ctx
    _post(client, "slowSql", [{"sqlText": "SELECT 2", "costMs": 900, "executeTime": T0}])
    with factory() as s:
        assert s.query(SlowSqlModel).first().stress_task_id is None


def test_report_invalid_stress_task_id_ignored(ctx):
    client, factory = ctx
    _post(client, "slowSql", [{"sqlText": "SELECT 3", "costMs": 900,
                               "executeTime": T0, "stressTaskId": "drop-table"}])
    with factory() as s:
        assert s.query(SlowSqlModel).first().stress_task_id is None


# ---------------------------------------------------------- 按染色精确反查


def test_query_by_stress_task_id_isolates_traffic(ctx):
    client, factory = ctx
    dt = datetime.fromtimestamp(T0 / 1000.0)
    with factory() as s:
        s.add_all([
            SlowSqlModel(app_name="demo-app", sql_text="stress sql", cost_ms=900,
                         source="jdbc", db_host="", execute_time=dt, stress_task_id=66),
            SlowSqlModel(app_name="demo-app", sql_text="background sql", cost_ms=800,
                         source="jdbc", db_host="", execute_time=dt, stress_task_id=None),
            SlowSqlModel(app_name="demo-app", sql_text="other task sql", cost_ms=700,
                         source="jdbc", db_host="", execute_time=dt, stress_task_id=77),
        ])
        s.commit()
    data = client.get("/api/slow-sqls", params={"stressTaskId": 66}).json()["data"]
    assert [i["sqlText"] for i in data["items"]] == ["stress sql"]
    assert data["items"][0]["stressTaskId"] == 66
    # 不带参数返回全部（旧行为兼容）
    all_rows = client.get("/api/slow-sqls").json()["data"]
    assert len(all_rows["items"]) == 3


def test_query_traces_and_commands_by_stress_task_id(ctx):
    client, factory = ctx
    dt = datetime.fromtimestamp(T0 / 1000.0)
    with factory() as s:
        s.add_all([
            TraceChainModel(trace_id="t-a", app_name="demo-app", root_method="C#stress",
                            trace_json="{}", total_ms=10, start_time=dt, stress_task_id=66),
            TraceChainModel(trace_id="t-b", app_name="demo-app", root_method="C#bg",
                            trace_json="{}", total_ms=10, start_time=dt, stress_task_id=None),
            SlowCommandModel(app_name="demo-app", type="redis", command="get x",
                             cost_ms=200, execute_time=dt, stress_task_id=66),
            SlowCommandModel(app_name="demo-app", type="redis", command="get y",
                             cost_ms=200, execute_time=dt, stress_task_id=77),
        ])
        s.commit()
    traces = client.get("/api/traces", params={"stressTaskId": 66}).json()["data"]
    assert [i["traceId"] for i in traces["items"]] == ["t-a"]
    cmds = client.get("/api/slow-commands", params={"stressTaskId": 66}).json()["data"]
    assert [i["command"] for i in cmds["items"]] == ["get x"]
