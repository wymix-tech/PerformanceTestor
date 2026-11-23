"""观测查询读路径测试：/api/apps、/api/jvm/metrics、/api/traces、/api/slow-sqls、
/api/slow-commands、/api/hotspots（契约对齐 perf-web/src/api/index.ts）。

重点覆盖 handoff-main.md 锁定的游标分页契约：
- 同秒多行不跳批：(time < ct) OR (time = ct AND id < cid)；
- hasMore 判定：LIMIT size+1，nextCursor 取截断后最后一条；
- 时间参数兼容 epoch 毫秒与 ISO-8601 字符串。
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.collector_repo import (
    CollectorRepository,
    HotspotMethodModel,
    JvmMetricModel,
    SlowCommandModel,
    SlowSqlModel,
    TraceChainModel,
)
from server.cursor import decode_cursor
from server.db import make_engine
from server.stress_repo import Base, StressRepository

T0 = 1794572783584  # epoch ms（2026-11-13T12:26:23 本地时区）


def _factory() -> sessionmaker:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    return factory


@pytest.fixture()
def ctx():
    factory = _factory()
    client = TestClient(create_app(stress_repo=StressRepository(factory),
                                   collector_repo=CollectorRepository(factory),
                                   auth_enabled=False))
    return client, factory


def _seed(factory, rows):
    with factory() as s:
        s.add_all(rows)
        s.commit()


def _pages(client: TestClient, url: str, params: dict, size: int = 2):
    """连续翻页直到 hasMore=False，返回全部 items 与末页响应。"""
    items, last = [], None
    cursor = None
    while True:
        p = dict(params, size=size)
        if cursor:
            p["cursor"] = cursor
        last = client.get(url, params=p).json()["data"]
        items.extend(last["items"])
        if not last["hasMore"]:
            break
        cursor = last["nextCursor"]
    return items, last


# ---------------------------------------------------------------- /api/apps

def test_apps_order_and_camel_fields(ctx):
    client, factory = ctx
    from datetime import datetime as _dt

    from server.collector_repo import AppInstanceModel
    _seed(factory, [
        AppInstanceModel(app_name="app-b", app_host="h2", app_port=8081,
                         app_version="2.0", agent_version="a2", status=1,
                         last_report_time=_dt(2026, 11, 13, 12, 0, 0)),
        AppInstanceModel(app_name="app-a", app_host="h1", app_port=8080,
                         app_version="1.0", agent_version="a1", status=1,
                         last_report_time=_dt(2026, 11, 13, 12, 26, 23)),
    ])
    data = client.get("/api/apps").json()["data"]
    assert [d["appName"] for d in data] == ["app-a", "app-b"]  # lastReportTime DESC 兜底
    assert set(data[0]) >= {"appName", "appHost", "appPort", "appVersion",
                            "status", "lastReportTime"}
    assert data[0]["status"] == 1


# ------------------------------------------------------------ /api/jvm/metrics

def test_jvm_metrics_cursor_paging_and_epoch_ms(ctx):
    client, factory = ctx
    from datetime import datetime
    base = datetime.fromtimestamp(T0 / 1000.0)
    _seed(factory, [
        JvmMetricModel(app_name="demo", collect_time=base,
                       heap_used_mb=100 + i, heap_max_mb=512,
                       young_gc_count=i, cpu_usage=10.0)
        for i in range(5)
    ])
    # start/end 传 epoch 毫秒（前端 Date.now() 形态）
    items, _ = _pages(client, "/api/jvm/metrics",
                      {"app": "demo", "start": T0 - 60_000, "end": T0 + 60_000})
    assert [i["heapUsedMb"] for i in items] == [104, 103, 102, 101, 100]  # 时间倒序
    assert items[0]["collectTime"].startswith("2026-")  # ISO 秒级

    # size=2 游标翻页合并 = 全量倒序
    paged, last = _pages(client, "/api/jvm/metrics", {"app": "demo"})
    assert paged == items and last["nextCursor"] is None and last["hasMore"] is False
    # 下一页游标解码后指向"截断后最后一条"而非多取那条
    first = client.get("/api/jvm/metrics", params={"app": "demo", "size": 2}).json()["data"]
    ct, cid = decode_cursor(first["nextCursor"])
    assert cid == items[1]["id"] and first["items"][1]["id"] == cid


def test_jvm_metrics_limit_fallback_and_clamp(ctx):
    client, _ = ctx
    # 未传 size 回退 limit（旧参数兼容）
    assert client.get("/api/jvm/metrics", params={"limit": 3}).json()["code"] == 0
    # 上限 500 钳制（size=9999 不报错）
    assert client.get("/api/jvm/metrics", params={"size": 9999}).json()["code"] == 0
    # 坏游标 → 全局异常包装 code=1
    body = client.get("/api/jvm/metrics", params={"cursor": "!!bad!!"}).json()
    assert body["code"] == 1


# ----------------------------------------------------------- /api/traces(+/{id})

def test_traces_list_filter_and_detail(ctx):
    client, factory = ctx
    from datetime import datetime
    t = datetime.fromtimestamp(T0 / 1000.0)
    _seed(factory, [
        TraceChainModel(trace_id="t1", app_name="demo", root_method="OrderController#create",
                        trace_json=json.dumps({"node": "root"}), total_ms=120.5, start_time=t),
        TraceChainModel(trace_id="t2", app_name="demo", root_method="UserController#list",
                        trace_json="{}", total_ms=30.0, start_time=t),
        TraceChainModel(trace_id="t3", app_name="other", root_method="OrderController#create",
                        trace_json="{}", total_ms=999.0, start_time=t),
    ])
    data = client.get("/api/traces", params={"app": "demo", "method": "Order",
                                            "minCostMs": 100}).json()["data"]["items"]
    assert [i["traceId"] for i in data] == ["t1"]
    assert set(data[0]) == {"id", "traceId", "appName", "rootMethod", "totalMs",
                            "startTime", "stressTaskId",
                            "spanId", "parentSpanId"}  # P2-2 微服务串联字段
    assert "traceJson" not in data[0]  # 列表不携带调用树

    detail = client.get("/api/traces/t1").json()
    assert detail["code"] == 0
    assert detail["data"]["traceJson"] == '{"node": "root"}'
    assert client.get("/api/traces/nope").json()["code"] == 1


# ------------------------------------------------------------- /api/slow-sqls

def test_slow_sql_same_second_cursor_no_skip():
    """同秒游标不跳批：5 条相同 execute_time 递增 id，size=2 翻 3 页合并恰好 5 条。"""
    factory = _factory()
    client = TestClient(create_app(stress_repo=StressRepository(factory),
                                   collector_repo=CollectorRepository(factory),
                                   auth_enabled=False))
    from datetime import datetime
    t = datetime.fromtimestamp(T0 / 1000.0)
    _seed(factory, [
        SlowSqlModel(app_name="demo", sql_text=f"SELECT {i}", cost_ms=100 + i,
                     source="jdbc", db_host="db1", execute_time=t)
        for i in range(5)
    ])
    merged, last = _pages(client, "/api/slow-sqls", {"app": "demo"})
    assert len(merged) == 5
    assert len({i["id"] for i in merged}) == 5  # 无重复
    assert [i["costMs"] for i in merged] == [104, 103, 102, 101, 100]  # 无遗漏，时间倒序
    assert last["hasMore"] is False and last["nextCursor"] is None
    assert set(merged[0]) >= {"sqlText", "costMs", "source", "dbHost",
                              "methodChain", "executeTime"}


def test_slow_sql_min_cost_and_time_range(ctx):
    client, factory = ctx
    from datetime import datetime, timedelta
    t = datetime.fromtimestamp(T0 / 1000.0)
    _seed(factory, [
        SlowSqlModel(app_name="demo", sql_text="A", cost_ms=500,
                     execute_time=t),
        SlowSqlModel(app_name="demo", sql_text="B", cost_ms=100,
                     execute_time=t + timedelta(minutes=1)),
    ])
    data = client.get("/api/slow-sqls", params={
        "minCostMs": 200, "start": T0, "end": T0 + 60_000}).json()["data"]["items"]
    assert [i["sqlText"] for i in data] == ["A"]


# --------------------------------------------------------- /api/slow-commands

def test_slow_commands_type_filter(ctx):
    client, factory = ctx
    from datetime import datetime
    t = datetime.fromtimestamp(T0 / 1000.0)
    _seed(factory, [
        SlowCommandModel(app_name="demo", type="redis", command="GET k",
                         cost_ms=80, trace_id="t1", execute_time=t),
        SlowCommandModel(app_name="demo", type="es", command="search",
                         cost_ms=200, execute_time=t),
    ])
    data = client.get("/api/slow-commands", params={"type": "REDIS"}).json()["data"]["items"]
    assert [i["command"] for i in data] == ["GET k"]  # type 大小写归一
    assert set(data[0]) >= {"type", "command", "costMs", "traceId", "methodChain",
                            "executeTime"}


# ------------------------------------------------------------- /api/hotspots

def test_hotspots_aggregate_and_default_window(ctx):
    client, factory = ctx
    from datetime import datetime, timedelta
    now = datetime.now()
    _seed(factory, [
        HotspotMethodModel(app_name="demo", method_name="A#m", samples=30,
                           sample_time=now - timedelta(minutes=10)),
        HotspotMethodModel(app_name="demo", method_name="A#m", samples=12,
                           sample_time=now - timedelta(minutes=5)),
        HotspotMethodModel(app_name="demo", method_name="B#m", samples=100,
                           sample_time=now - timedelta(hours=2)),  # 窗口外
    ])
    data = client.get("/api/hotspots", params={"app": "demo"}).json()["data"]
    assert data == [{"methodName": "A#m", "samples": 42}]  # 缺省最近 1 小时窗口聚合
