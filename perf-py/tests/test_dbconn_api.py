"""dbconn 域（数据库连接管理）协议与行为测试。

覆盖：列表密码不回显 / 新建校验 / 编辑留空密码沿用 / host 自动推导 /
默认连接概念移除（isDefault 入参被忽略）/ 删除不存在 / 连通性测试。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.collector_repo import DbConnModel, SourceRepoModel
from server.crypto import decrypt
from server.db import make_engine
from server.dbconn_service import DbConnService
from server.stress_repo import Base, StressRepository


def _factory() -> sessionmaker:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    with factory() as s:
        s.add(SourceRepoModel(repo_name="demo-app", repo_type="zip", source_path="/tmp"))
        s.commit()
    return factory


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, *args):
        self.last_sql = sql

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self):
        self.closed = False

    def cursor(self):
        return _FakeCursor()

    def close(self):
        self.closed = True


@pytest.fixture()
def ctx():
    factory = _factory()
    app = create_app(stress_repo=StressRepository(factory),
                     dbconn_service=DbConnService(factory),
                     auth_enabled=False)
    return TestClient(app), factory


def _all(factory):
    with factory() as s:
        return list(s.scalars(select(DbConnModel).order_by(DbConnModel.id)))


def _row_of(factory, conn_id):
    with factory() as s:
        return s.get(DbConnModel, conn_id)


# ---------------------------------------------------------------- 列表与保存

def test_save_and_list_masks_password(ctx, monkeypatch):
    monkeypatch.setenv("PERF_CRYPTO_SECRET", "s3cret")
    client, factory = ctx
    resp = client.post("/api/dbconns", json={
        "connName": "demo-主库", "dbUrl": "jdbc:mysql://127.0.0.1:3306/demo",
        "username": "root", "password": "secret", "isDefault": 1})
    body = resp.json()
    assert body["code"] == 0
    conn = body["data"]
    assert conn["connName"] == "demo-主库"
    # host 留空自动从 URL 推导（与慢 SQL dbHost 匹配）
    assert conn["host"] == "127.0.0.1:3306/demo"
    assert conn["password"] == "", "保存响应不回显密码"

    rows = _all(factory)
    assert len(rows) == 1
    assert rows[0].password != "secret"
    assert decrypt(rows[0].password) == "secret", "密码加密入库且可解密"
    assert rows[0].is_default == 0, "默认连接概念已移除，isDefault 入参被忽略"

    listed = client.get("/api/dbconns").json()["data"]
    assert listed[0]["password"] == "", "列表不返回明文"
    assert listed[0]["isDefault"] == 0


def test_save_validations(ctx):
    client, _ = ctx
    cases = [
        ({"dbUrl": "jdbc:mysql://h:3306/d", "username": "root"}, "连接名称不能为空"),
        ({"connName": "x", "username": "root"},
         "JDBC URL 不能为空（如 jdbc:mysql://127.0.0.1:3306/demo）"),
        ({"connName": "x", "dbUrl": "jdbc:postgresql://h:5432/d", "username": "root"},
         "JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:mariadb://"),
        ({"connName": "x", "dbUrl": "jdbc:mysql://h:3306/d"}, "用户名不能为空"),
    ]
    for payload, msg in cases:
        body = client.post("/api/dbconns", json=payload).json()
        assert body == {"code": 1, "message": msg, "data": None}


def test_edit_keeps_password_when_blank_and_app_supports_auto_conn(ctx):
    client, factory = ctx
    # 模拟 agent 自动注册：空凭据连接
    with factory() as s:
        s.add(DbConnModel(conn_name="自动关联-demo-app", host="127.0.0.1:3306/demo",
                          app_name="demo-app",
                          db_url="jdbc:mysql://127.0.0.1:3306/demo?useUnicode=true",
                          username="", password="", is_default=0))
        s.commit()
    conn_id = _all(factory)[0].id

    # 后补凭据（编辑，host 留空沿用）
    body = client.post("/api/dbconns", json={
        "id": conn_id, "connName": "demo-主库", "appName": "demo-app",
        "dbUrl": "jdbc:mysql://127.0.0.1:3306/demo",
        "username": "root", "password": ""}).json()
    assert body["code"] == 0
    assert body["data"]["id"] == conn_id

    row = _row_of(factory, conn_id)
    assert row.username == "root"
    assert row.password == "", "密码留空沿用已存凭据（原本为空仍为空）"
    assert row.host == "127.0.0.1:3306/demo", "host 留空时由 URL 重新推导"

    # 编辑时给出新密码则覆盖
    client.post("/api/dbconns", json={
        "id": conn_id, "connName": "demo-主库", "dbUrl": "jdbc:mysql://127.0.0.1:3306/demo",
        "username": "root", "password": "new-pass"})
    assert decrypt(_row_of(factory, conn_id).password) == "new-pass"


def test_edit_nonexistent_conn(ctx):
    client, _ = ctx
    body = client.post("/api/dbconns", json={
        "id": 999, "connName": "x", "dbUrl": "jdbc:mysql://h:3306/d",
        "username": "root"}).json()
    assert body["message"] == "连接不存在: 999"


# ---------------------------------------------------------------- 删除

def test_legacy_default_flag_zeroed_on_edit(ctx):
    """存量 is_default=1 的连接编辑保存后归零（默认连接概念已移除）。"""
    client, factory = ctx
    with factory() as s:
        s.add(DbConnModel(conn_name="旧默认", db_url="jdbc:mysql://h:3306/d",
                          username="root", is_default=1))
        s.commit()
    conn_id = _all(factory)[0].id
    client.post("/api/dbconns", json={
        "id": conn_id, "connName": "旧默认-改", "dbUrl": "jdbc:mysql://h:3306/d",
        "username": "root"})
    assert _row_of(factory, conn_id).is_default == 0


def test_delete_conn(ctx):
    client, factory = ctx
    conn_id = client.post("/api/dbconns", json={
        "connName": "tmp", "dbUrl": "jdbc:mysql://h:3306/d",
        "username": "root"}).json()["data"]["id"]
    assert client.delete(f"/api/dbconns/{conn_id}").json()["code"] == 0
    assert _all(factory) == []
    assert client.delete(f"/api/dbconns/{conn_id}").json()["message"] \
        == f"连接不存在: {conn_id}"


# ---------------------------------------------------------------- 连通性测试

def test_test_connection_with_injected_connect_fn(ctx):
    client, factory = ctx
    opened = []

    def connect_fn(url, user, password):
        opened.append((url, user, password))
        return _FakeConn()

    app = client.app
    app.state.dbconn_service._connect_fn = connect_fn

    body = client.post("/api/dbconns/test", json={
        "dbUrl": "jdbc:mysql://127.0.0.1:3306/demo",
        "username": "root", "password": "secret"}).json()
    assert body == {"code": 0, "message": "success", "data": {"ok": True}}
    assert opened == [("jdbc:mysql://127.0.0.1:3306/demo", "root", "secret")]

    # 带 SQL：试跑 EXPLAIN
    body = client.post("/api/dbconns/test", json={
        "dbUrl": "jdbc:mysql://127.0.0.1:3306/demo", "username": "root",
        "sqlText": "SELECT 1"}).json()
    assert body["data"]["ok"] is True


def test_test_connection_failure_and_validation(ctx):
    client, _ = ctx

    def failing_connect(url, user, password):
        raise RuntimeError("Access denied for user 'root'")

    client.app.state.dbconn_service._connect_fn = failing_connect
    body = client.post("/api/dbconns/test", json={
        "dbUrl": "jdbc:mysql://127.0.0.1:3306/demo",
        "username": "root", "password": "bad"}).json()
    assert body["data"]["ok"] is False
    assert "Access denied" in body["data"]["error"]

    # 缺参 / 非 MySQL URL / 受限 SQL
    assert client.post("/api/dbconns/test", json={"username": "root"}).json() \
        ["data"]["error"] == "请填写 JDBC URL"
    assert client.post("/api/dbconns/test", json={
        "dbUrl": "jdbc:mysql://h:3306/d", "username": "root",
        "sqlText": "DELETE FROM t"}).json()["data"]["error"] == "仅支持对 SELECT/WITH 查询执行 EXPLAIN"
