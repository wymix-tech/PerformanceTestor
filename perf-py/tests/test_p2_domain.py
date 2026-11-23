"""P2-2 域测试：SQL 脱敏指纹、W3C span 串联落库与查询、慢 SQL 指纹聚合。"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.collector_repo import (
    CollectorRepository,
    SlowSqlModel,
    SourceRepoModel,
    TraceChainModel,
)
from server.sql_fingerprint import mask_sensitive, sql_fingerprint
from server.stress_repo import Base, StressRepository

EPOCH = 1794572783584
NOW_DT = datetime.fromtimestamp(EPOCH / 1000.0)


def _factory() -> sessionmaker:
    from server.db import make_engine

    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    with factory() as s:
        s.add_all([
            SourceRepoModel(repo_name="demo-app", repo_type="zip", source_path="/tmp"),
            SourceRepoModel(repo_name="svc2", repo_type="zip", source_path="/tmp"),
        ])
        s.commit()
    return factory


@pytest.fixture()
def ctx():
    factory = _factory()
    client = TestClient(create_app(stress_repo=StressRepository(factory),
                                   collector_repo=CollectorRepository(factory),
                                   auth_enabled=False))
    return client, factory


# ---------------------------------------------------------------- 脱敏与指纹

def test_mask_sensitive_literals():
    masked = mask_sensitive("SELECT * FROM users WHERE phone='13812345678' AND email='a.b@x.com'")
    assert "13812345678" not in masked and "a.b@x.com" not in masked
    assert "'***'" in masked
    masked2 = mask_sensitive("UPDATE t SET password='p@ss word' WHERE id=1")
    assert "p@ss word" not in masked2
    masked3 = mask_sensitive("SELECT * FROM t WHERE id_card='110101199003078512'")
    assert "110101199003078512" not in masked3


def test_fingerprint_stable_across_parameter_variants():
    variants = []
    for i in range(100):
        variants.append(
            f"SELECT * FROM orders WHERE user_id={i} AND status IN ('paid','shipped')"
            f" AND remark='订单{i}备注' AND amount={i * 1.5}")
    fps = {sql_fingerprint(v) for v in variants}
    assert len(fps) == 1, "同模板不同参数应归为同一指纹"
    assert len(next(iter(fps))) == 16  # SHA-1 前 16 位 hex


def test_fingerprint_differs_for_different_templates():
    assert sql_fingerprint("SELECT * FROM a WHERE id=1") != \
        sql_fingerprint("SELECT * FROM b WHERE id=1")
    # IN 列表长度不同不影响模板归类
    assert sql_fingerprint("SELECT * FROM t WHERE id IN (1,2)") == \
        sql_fingerprint("SELECT * FROM t WHERE id IN (1,2,3,4)")


# ---------------------------------------------------------------- W3C span 串联

def _post_trace(client, trace_id, span_id, parent_span_id, app="demo-app"):
    return client.post("/api/agent/report", json={"type": "trace", "appName": app,
        "items": [{"traceId": trace_id, "spanId": span_id, "parentSpanId": parent_span_id,
                   "rootMethod": "Api#call", "totalMs": 100.0, "startTime": EPOCH, "tree": {}}]})


def test_cross_service_trace_spans_persist(ctx):
    client, factory = ctx
    # A → B 跨服务：同一 traceId，两个服务独立 span
    r1 = _post_trace(client, "a" * 32, "b" * 16, None)
    r2 = _post_trace(client, "a" * 32, "c" * 16, "b" * 16, app="svc2")
    assert r1.json()["code"] == 0 and r2.json()["code"] == 0
    with factory() as s:
        rows = list(s.scalars(select(TraceChainModel).order_by(TraceChainModel.id)))
    assert len(rows) == 2  # B 的 span 不再覆盖 A（旧 uk_trace 语义会丢失一条）
    assert {r.span_id for r in rows} == {"b" * 16, "c" * 16}
    assert rows[1].parent_span_id == "b" * 16


def test_legacy_trace_defaults_span_root_and_replay_idempotent(ctx):
    client, factory = ctx
    body = {"type": "trace", "appName": "demo-app",
            "items": [{"traceId": "t-old", "rootMethod": "Api#call", "totalMs": 1.0,
                       "startTime": EPOCH, "tree": {}}]}  # 旧探针：无 span 字段
    assert client.post("/api/agent/report", json=body).json()["code"] == 0
    assert client.post("/api/agent/report", json=body).json()["code"] == 0  # 重放幂等
    with factory() as s:
        rows = list(s.scalars(select(TraceChainModel)))
    assert len(rows) == 1 and rows[0].span_id == "root" and rows[0].parent_span_id is None


def test_trace_detail_returns_child_spans(ctx):
    client, _ = ctx
    _post_trace(client, "a" * 32, "b" * 16, None)
    _post_trace(client, "a" * 32, "c" * 16, "b" * 16, app="svc2")
    detail = client.get("/api/traces/" + "a" * 32).json()["data"]
    assert detail["spanId"] == "b" * 16  # 主链路 = 无上游 parent 的 span
    assert len(detail["childSpans"]) == 1
    child = detail["childSpans"][0]
    assert child["appName"] == "svc2" and child["parentSpanId"] == "b" * 16


# ---------------------------------------------------------------- 慢 SQL 落库脱敏指纹与聚合

def _post_slow_sqls(client, n=5):
    items = []
    for i in range(n):
        items.append({"sqlText": f"SELECT * FROM orders WHERE user_id={i} AND phone='1381234567{i}'",
                      "costMs": 100.0 + i * 10, "executeTime": EPOCH + i})
    return client.post("/api/agent/report", json={"type": "slowSql", "appName": "demo-app",
                                                  "items": items})


def test_slow_sql_masked_and_fingerprinted(ctx):
    client, factory = ctx
    assert _post_slow_sqls(client).json()["code"] == 0
    with factory() as s:
        rows = list(s.scalars(select(SlowSqlModel)))
    assert len(rows) == 5
    for r in rows:
        assert "1381234567" not in r.sql_text  # 手机号已脱敏
        assert r.sql_fingerprint and len(r.sql_fingerprint) == 16
    assert len({r.sql_fingerprint for r in rows}) == 1  # 同模板归一


def test_slow_sql_aggregate_endpoint(ctx):
    client, _ = ctx
    _post_slow_sqls(client)
    data = client.get("/api/slow-sqls/aggregate",
                      params={"app": "demo-app", "start": "0"}).json()["data"]
    assert len(data) == 1
    g = data[0]
    assert g["count"] == 5
    assert g["avgCostMs"] == 120.0  # (100+110+120+130+140)/5
    assert g["p95CostMs"] == 140.0
    assert "1381234567" not in g["sampleSql"]
