"""collector 域协议与行为测试（阶段 5 · 步骤 3）。

Java 服务未运行、collector 为写路径（POST），golden 黑盒捕获暂不可得——
契约以 Java 源码为准（沿用 ai_knowledge 空库 golden 的源码级契约先例）：
- wire 响应逐字段断言（鉴权 401 固定体 / PROJECT_NOT_FOUND 404 包装 / success ok 包装）；
- 落库行为逐条对照 AgentReportService 语义（心跳 upsert、五类数据容错、自动注册连接）。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.collector_repo import (
    AppInstanceModel,
    CollectorRepository,
    DbConnModel,
    HotspotMethodModel,
    JvmMetricModel,
    SlowCommandModel,
    SlowSqlModel,
    SourceRepoModel,
    TraceChainModel,
)
from server.collector_service import to_local_datetime
from server.db import make_engine
from server.stress_repo import Base, StressRepository

EPOCH = 1794572783584  # 2026-11-13T12:26:23 本地时区（与 golden 同源毫秒值）


def _factory() -> sessionmaker:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    with factory() as s:
        s.add(SourceRepoModel(repo_name="demo-app", repo_type="zip", source_path="/tmp"))
        s.commit()
    return factory


@pytest.fixture()
def ctx():
    factory = _factory()
    client = TestClient(create_app(stress_repo=StressRepository(factory),
                                   collector_repo=CollectorRepository(factory)))
    return client, factory


def _post(client: TestClient, body: dict, token: str | None = None):
    headers = {"X-Perf-Agent-Token": token} if token is not None else {}
    return client.post("/api/agent/report", json=body, headers=headers)


def _all(factory, model):
    with factory() as s:
        return list(s.scalars(select(model).order_by(model.id)))


# ---------------------------------------------------------------- wire 协议

def test_report_success_wire_format(ctx):
    """成功响应：ApiResponse.ok() → {"code":0,"message":"success","data":null}。"""
    client, _ = ctx
    resp = _post(client, {"type": "jvm", "appName": "demo-app", "items": []})
    assert resp.status_code == 200
    assert resp.json() == {"code": 0, "message": "success", "data": None}


def test_project_not_found_wire_format(ctx):
    """项目不存在：HTTP 200 + code=404 固定 message（agent 识别后自动停报）。"""
    client, _ = ctx
    resp = _post(client, {"type": "jvm", "appName": "ghost-app", "items": []})
    assert resp.status_code == 200
    assert resp.json() == {"code": 404,
                           "message": "PROJECT_NOT_FOUND: 项目「ghost-app」不存在于平台，数据已全部丢弃",
                           "data": None}


def test_project_fallback_to_appname_and_null_semantics(ctx):
    """targetProjectName 缺省回退 appName；值为 JSON null 时按 Java String.valueOf → "null"。"""
    client, _ = ctx
    # 回退 appName（已注册项目）→ 放行
    assert _post(client, {"type": "jvm", "appName": "demo-app", "items": []}).json()["code"] == 0
    # targetProjectName=null → project "null" → 不存在 → 404
    resp = _post(client, {"type": "jvm", "appName": "demo-app",
                          "targetProjectName": None, "items": []})
    assert resp.json()["code"] == 404 and "「null」" in resp.json()["message"]


def test_agent_token_auth_wire_format(ctx):
    """令牌鉴权：配置后缺头/错头 → HTTP 401 + 固定体（逐字节一致）；对头放行。"""
    client, factory = ctx
    app = client.app
    app.state.agent_token = "s3cret"
    app.state.agent_auth_enabled = True

    resp = client.post("/api/agent/report", json={"type": "jvm", "appName": "demo-app"})
    assert resp.status_code == 401
    assert resp.json() == {"code": 401, "message": "探针令牌无效", "data": None}

    resp = _post(client, {"type": "jvm", "appName": "demo-app", "items": []}, token="wrong")
    assert resp.status_code == 401

    resp = _post(client, {"type": "jvm", "appName": "demo-app", "items": []}, token="s3cret")
    assert resp.json()["code"] == 0
    assert len(_all(factory, JvmMetricModel)) == 0  # 空 items：心跳注册但不落数据


def test_agent_token_unset_keeps_open(ctx):
    """令牌未配置（默认空）：保持开放，兼容存量 agent（对齐拦截器放行分支）。"""
    client, _ = ctx
    client.app.state.agent_token = ""
    resp = client.post("/api/agent/report", json={"type": "jvm", "appName": "demo-app"})
    assert resp.json()["code"] == 0


def test_auth_checked_before_project_admission(ctx):
    """鉴权先于项目准入（对齐拦截器 preHandle 在 Controller 之前）。"""
    client, _ = ctx
    client.app.state.agent_token = "t"
    resp = client.post("/api/agent/report", json={"appName": "ghost-app"})
    assert resp.status_code == 401, "未过鉴权不应触达项目准入逻辑"


# ---------------------------------------------------------------- 心跳注册

def test_app_instance_upsert_semantics(ctx):
    """心跳 upsert：host/agent_version/心跳时间始终覆盖；未带端口/版本保留库中已有值。"""
    client, factory = ctx
    _post(client, {"type": "jvm", "appName": "demo-app", "items": [],
                   "appPort": 8081, "appVersion": "v1.2"})
    inst = _all(factory, AppInstanceModel)[0]
    assert (inst.app_name, inst.app_host, inst.app_port,
            inst.app_version, inst.agent_version, inst.status) == \
        ("demo-app", "testclient", 8081, "v1.2", "1.0.0", 1)
    first_heartbeat = inst.last_report_time

    # 二次上报：不带端口/版本 → 保留
    _post(client, {"type": "jvm", "appName": "demo-app", "items": []})
    inst2 = _all(factory, AppInstanceModel)[0]
    assert inst2.app_port == 8081 and inst2.app_version == "v1.2", "未携带时保留已有值"
    assert inst2.agent_version == "1.0.0" and inst2.status == 1
    assert inst2.last_report_time >= first_heartbeat, "心跳时间始终更新"


def test_app_instance_port_zero_or_version_null_keeps_existing(ctx):
    """appPort<=0 → 不覆盖；appVersion=null → 空串不覆盖（对齐 IF(VALUES(...)) 分支）。"""
    client, factory = ctx
    _post(client, {"type": "jvm", "appName": "demo-app", "items": [],
                   "appPort": 8081, "appVersion": "v1"})
    _post(client, {"type": "jvm", "appName": "demo-app", "items": [],
                   "appPort": 0, "appVersion": None})
    inst = _all(factory, AppInstanceModel)[0]
    assert inst.app_port == 8081 and inst.app_version == "v1"


# ---------------------------------------------------------------- 五类数据落库

def test_jvm_metrics_defaults_and_time_conversion(ctx):
    """jvm：缺失字段按 0 兜底；collectTime epoch 毫秒 → 本地时区 datetime。"""
    client, factory = ctx
    _post(client, {"type": "jvm", "appName": "demo-app", "items": [
        {"collectTime": EPOCH, "heapUsedMb": 512.5},
        "bad-item",  # 非 dict 容错跳过
    ]})
    rows = _all(factory, JvmMetricModel)
    assert len(rows) == 1
    r = rows[0]
    assert r.collect_time == to_local_datetime(EPOCH)
    assert r.heap_used_mb == 512.5
    assert (r.heap_max_mb, r.young_gc_count, r.thread_count,
            r.cpu_usage, r.system_cpu) == (0.0, 0, 0, 0.0, 0.0)


def test_trace_compact_json_and_replay_idempotent(ctx):
    """trace：tree 序列化为紧凑 JSON（无空格，对齐 Jackson）；缺 tree → "{}"；
    trace_id 冲突时更新（重放幂等）。"""
    client, factory = ctx
    body = {"type": "trace", "appName": "demo-app", "items": [
        {"traceId": "t-1", "rootMethod": "OrderController#list", "totalMs": 120.5,
         "startTime": EPOCH, "tree": {"name": "root", "children": [{"name": "db"}]}},
        {"traceId": "t-2", "rootMethod": "UserController#list", "startTime": EPOCH},
    ]}
    _post(client, body)
    rows = {r.trace_id: r for r in _all(factory, TraceChainModel)}
    assert set(rows) == {"t-1", "t-2"}
    assert rows["t-1"].trace_json == '{"name":"root","children":[{"name":"db"}]}'
    assert rows["t-2"].trace_json == "{}", "缺 tree → {}（对齐 Java 兜底）"
    assert rows["t-1"].start_time == to_local_datetime(EPOCH)

    # 重放同 traceId：更新而非新增
    replay = {"type": "trace", "appName": "demo-app", "items": [
        {"traceId": "t-1", "rootMethod": "OrderController#list", "totalMs": 99.0,
         "startTime": EPOCH, "tree": {"name": "root2"}}]}
    _post(client, replay)
    rows = {r.trace_id: r for r in _all(factory, TraceChainModel)}
    assert len(rows) == 2
    assert rows["t-1"].total_ms == 99.0 and rows["t-1"].trace_json == '{"name":"root2"}'


def test_slow_sql_source_default_and_auto_db_conn(ctx):
    """slowSql：source 缺省 jdbc；dbHost 自动注册连接且同批同地址仅一次；已关联不重复。"""
    client, factory = ctx
    item = {"sqlText": "SELECT * FROM perf_order", "costMs": 1500.0,
            "dbHost": "10.0.0.20:3306/perf", "executeTime": EPOCH,
            "methodChain": ["OrderController", "OrderService"]}
    _post(client, {"type": "slowSql", "appName": "demo-app", "items": [item, dict(item)]})
    rows = _all(factory, SlowSqlModel)
    assert len(rows) == 2
    assert rows[0].source == "jdbc" and rows[0].db_host == "10.0.0.20:3306/perf"
    assert rows[0].method_chain == '["OrderController","OrderService"]'

    conns = _all(factory, DbConnModel)
    assert len(conns) == 1, "同批同库地址只自动注册一次"
    c = conns[0]
    assert c.conn_name == "自动关联-demo-app"
    assert c.db_url == ("jdbc:mysql://10.0.0.20:3306/perf"
                        "?useUnicode=true&characterEncoding=utf8"
                        "&serverTimezone=Asia/Shanghai&rewriteBatchedStatements=true")
    assert c.username == "" and c.is_default == 0

    # 再次上报同地址：已有 appName 绑定 → 不重复插入
    _post(client, {"type": "slowSql", "appName": "demo-app",
                   "items": [{"sqlText": "SELECT 1", "costMs": 1.0,
                              "dbHost": "10.0.0.20:3306/perf", "executeTime": EPOCH}]})
    assert len(_all(factory, DbConnModel)) == 1


def test_slow_command_lowercase_skip_blank_and_truncate(ctx):
    """slowCommand：type 小写化、空 type 跳过、command 超 1000 截断加 ...。"""
    client, factory = ctx
    long_cmd = "GET " + "k" * 1100
    _post(client, {"type": "slowCommand", "appName": "demo-app", "items": [
        {"type": "Redis", "command": "GET key", "costMs": 88.0,
         "traceId": "t-9", "executeTime": EPOCH, "methodChain": ["A", "B"]},
        {"type": "", "command": "skip-me", "costMs": 1.0, "executeTime": EPOCH},
        {"type": "MQ", "command": long_cmd, "costMs": 2.0, "executeTime": EPOCH},
    ]})
    rows = _all(factory, SlowCommandModel)
    assert len(rows) == 2
    assert rows[0].type == "redis" and rows[0].command == "GET key"
    assert rows[1].command == "GET " + "k" * 996 + "...", "截断保留前 1000 字符加 ..."
    assert len(rows[1].command) == 1003


def test_hotspot_nested_aggregation(ctx):
    """hotspot：嵌套聚合结构；method 空或 samples<=0 跳过。"""
    client, factory = ctx
    _post(client, {"type": "hotspot", "appName": "demo-app", "items": [
        {"sampleTime": EPOCH, "items": [
            {"method": "com.demo.OrderService#list", "samples": 42},
            {"method": "com.demo.UserService#get", "samples": 7},
            {"method": "", "samples": 99},
            {"method": "com.demo.Skip#me", "samples": 0},
        ]},
        {"sampleTime": EPOCH, "items": []},
    ]})
    rows = _all(factory, HotspotMethodModel)
    # method 为空串（非 null）在 Java 侧同样保留——对齐 strVal + isNotNull 判断
    assert [r.method_name for r in rows] == ["com.demo.OrderService#list",
                                             "com.demo.UserService#get", ""]
    assert rows[0].samples == 42 and rows[0].sample_time == to_local_datetime(EPOCH)


# ---------------------------------------------------------------- 容错语义

def test_unknown_type_and_missing_items_ignored(ctx):
    """未知 type 忽略但响应仍 success（容错优先）；items 缺失仅心跳。"""
    client, factory = ctx
    resp = _post(client, {"type": "nope", "appName": "demo-app", "items": [1]})
    assert resp.json()["code"] == 0
    assert _all(factory, JvmMetricModel) == []


def test_missing_app_name_with_known_project_still_ok(ctx):
    """appName 缺失但 targetProjectName 指向已注册项目 → handleReport 忽略，响应 success。"""
    client, factory = ctx
    resp = _post(client, {"type": "jvm", "targetProjectName": "demo-app",
                          "items": [{"collectTime": EPOCH}]})
    assert resp.json()["code"] == 0
    assert _all(factory, JvmMetricModel) == []


def test_both_project_fields_missing_404(ctx):
    """appName 与 targetProjectName 均缺失 → project="" → 404（对齐 Java 预检）。"""
    client, _ = ctx
    resp = _post(client, {"type": "jvm", "items": []})
    assert resp.json()["code"] == 404 and "「」" in resp.json()["message"]


def test_dropped_counter_accepted(ctx):
    """agent 丢弃计数随报文带出：仅观测用，不影响落库与响应。"""
    client, factory = ctx
    resp = _post(client, {"type": "jvm", "appName": "demo-app",
                          "dropped": {"trace": 3}, "items": [{"collectTime": EPOCH}]})
    assert resp.json()["code"] == 0
    assert len(_all(factory, JvmMetricModel)) == 1


def test_per_item_failure_does_not_kill_batch(ctx):
    """单条 item 异常不影响整批（对齐 Java 逐条 try/catch）。"""
    client, factory = ctx
    _post(client, {"type": "jvm", "appName": "demo-app", "items": [
        {"collectTime": "not-a-number", "heapUsedMb": 1.0},  # collectTime 非数值 → 0 兜底不抛
        {"collectTime": EPOCH, "heapUsedMb": 2.0},
    ]})
    rows = _all(factory, JvmMetricModel)
    assert len(rows) == 2
