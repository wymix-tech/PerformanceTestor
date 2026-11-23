"""profile_snapshot 域测试（P2-1）：上报落库容错、折叠栈服务端合并、
/api/profiles 聚合查询与 AI profile_flamegraph_summary 工具。"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.collector_repo import (
    CollectorRepository,
    ProfileSnapshotModel,
    SourceRepoModel,
)
from server.profile_merge import folded_to_tree, merge_folded, parse_folded, top_call_paths
from server.stress_repo import Base, StressRepository

EPOCH = 1794572783584
NOW_DT = datetime.fromtimestamp(EPOCH / 1000.0)


def _factory() -> sessionmaker:
    from server.db import make_engine

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
                                   collector_repo=CollectorRepository(factory),
                                   auth_enabled=False))
    return client, factory


def _all(factory):
    with factory() as s:
        return list(s.scalars(
            select(ProfileSnapshotModel).order_by(ProfileSnapshotModel.id)))


def test_parse_folded_skips_bad_lines():
    stacks = parse_folded("a.B;m.C 3\nbadline\n\n  \na.B;m.C 2\nx.Y notnum")
    assert stacks == {"a.B;m.C": 5}


def test_merge_folded_accumulates_across_snapshots():
    merged = merge_folded(["a.B 1\nm.C;a.B 2", "a.B 3\nm.C;a.B 4"])
    assert merged["stacks"] == {"a.B": 4, "m.C;a.B": 6}
    assert merged["totalSamples"] == 10


def test_folded_to_tree_inclusive_and_self_values():
    tree = folded_to_tree({"a;b": 7, "a": 3})
    assert tree["name"] == "root" and tree["value"] == 10
    a = next(c for c in tree["children"] if c["name"] == "a")
    assert a["value"] == 10 and a["self"] == 3
    b = next(c for c in a["children"] if c["name"] == "b")
    assert b["value"] == 7 and b["self"] == 7


def test_folded_to_tree_overflow_caps_nodes():
    merged = {f"f{i};g{i};leaf{i}": 1 for i in range(100)}
    tree = folded_to_tree(merged, max_nodes=10)
    names = []

    def walk(n):
        names.append(n["name"])
        for c in n.get("children", []):
            walk(c)

    walk(tree)
    # 常规节点数封顶（10 + root），溢出路径全部聚入 *overflow* 桶，计数不丢
    assert "*overflow*" in names
    assert names.count("*overflow*") <= 30
    non_overflow = [n for n in names if n != "*overflow*" and n != "root"]
    assert len(non_overflow) <= 10
    assert tree["value"] == 100  # 样本总量不受截断影响


def test_top_call_paths_order_and_pct():
    items = top_call_paths({"a": 3, "b": 1, "c": 6}, top_n=2)
    assert [i["leaf"] for i in items] == ["c", "a"]
    assert items[0]["pct"] == 60.0


# ---------------------------------------------------------------- 上报落库

def _post(client, body):
    return client.post("/api/agent/report", json=body)


def test_report_profile_persists_snapshot(ctx):
    client, factory = ctx
    resp = _post(client, {"type": "profile", "appName": "demo-app", "items": [{
        "sampleTime": EPOCH, "profileType": "cpu",
        "foldedStacks": "com.demo.Api.handle;com.demo.Svc.run 12\njava.util.HashMap.get 5",
        "totalSamples": 17,
    }]})
    assert resp.json()["code"] == 0
    rows = _all(factory)
    assert len(rows) == 1
    assert rows[0].profile_type == "cpu"
    assert rows[0].total_samples == 17
    assert "com.demo.Svc.run" in rows[0].folded_stacks


def test_report_profile_rejects_bad_items(ctx):
    client, factory = ctx
    resp = _post(client, {"type": "profile", "appName": "demo-app", "items": [
        {"sampleTime": EPOCH, "profileType": "net", "foldedStacks": "a.B 1", "totalSamples": 1},
        {"sampleTime": EPOCH, "profileType": "alloc", "foldedStacks": "  ", "totalSamples": 0},
        {"sampleTime": EPOCH, "profileType": "lock", "foldedStacks": "a.B;b.C 9", "totalSamples": 9},
        "not-a-dict",
    ]})
    assert resp.json()["code"] == 0
    rows = _all(factory)
    assert len(rows) == 1 and rows[0].profile_type == "lock"


def test_report_profile_accepts_list_form_and_stress_task_id(ctx):
    client, factory = ctx
    resp = _post(client, {"type": "profile", "appName": "demo-app", "items": [{
        "sampleTime": EPOCH, "profileType": "alloc",
        "foldedStacks": ["a.B 10", "m.C;a.B 20"],
        "totalSamples": 30, "stressTaskId": "77",
    }]})
    assert resp.json()["code"] == 0
    row = _all(factory)[0]
    assert row.stress_task_id == 77 and "m.C;a.B 20" in row.folded_stacks


# ---------------------------------------------------------------- /api/profiles 聚合查询

def _seed_two_snapshots(factory):
    now = datetime.now()
    with factory() as s:
        s.add_all([
            ProfileSnapshotModel(app_name="demo-app", profile_type="cpu",
                                 folded_stacks="com.Api.list;com.Svc.load 4",
                                 total_samples=4, sample_time=now),
            ProfileSnapshotModel(app_name="demo-app", profile_type="cpu",
                                 folded_stacks="com.Api.list;com.Svc.load 6\ncom.Api.other 1",
                                 total_samples=7, sample_time=now),
        ])
        s.commit()


def test_profiles_api_merges_folded_format(ctx):
    client, factory = ctx
    _seed_two_snapshots(factory)
    resp = client.get("/api/profiles", params={"app": "demo-app", "profileType": "cpu",
                                               "format": "folded"})
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["totalSamples"] == 11 and data["uniqueStacks"] == 2
    assert data["snapshotCount"] == 2
    folded = data["folded"].splitlines()
    assert folded[0] == "com.Api.list;com.Svc.load 10"  # 跨批按栈路径合并累加
    assert "com.Api.other 1" in folded


def test_profiles_api_tree_format_and_filter(ctx):
    client, factory = ctx
    _seed_two_snapshots(factory)
    resp = client.get("/api/profiles", params={"profileType": "cpu", "format": "tree"})
    data = resp.json()["data"]
    tree = data["tree"]
    assert tree["value"] == 11
    api = next(c for c in tree["children"] if c["name"] == "com.Api.list")
    svc = next(c for c in api["children"] if c["name"] == "com.Svc.load")
    assert svc["value"] == 10 and svc["self"] == 10 and api["self"] == 0
    other = next(c for c in tree["children"] if c["name"] == "com.Api.other")
    assert other["self"] == 1
    # profileType 过滤：alloc 无数据返回空聚合
    resp = client.get("/api/profiles", params={"profileType": "alloc"})
    assert resp.json()["data"]["totalSamples"] == 0


def test_profiles_api_rejects_bad_profile_type(ctx):
    client, _ = ctx
    resp = client.get("/api/profiles", params={"profileType": "net"})
    # 全局异常处理器统一包装：HTTP 200 + code=1（对齐既有错误口径）
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 1
    assert "profileType" in body["message"]
