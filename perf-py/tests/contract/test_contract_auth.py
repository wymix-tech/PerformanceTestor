"""auth 域契约测试（阶段 5 · 步骤 5）：对照 golden auth_me + Java AuthController/JWT 拦截器语义。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.auth_service import AuthProperties, JwtService
from server.collector_repo import CollectorRepository
from server.db import make_engine
from server.source_repo import SourceRepository
from server.stress_repo import Base, StressRepository
from tests.contract._utils import assert_matches_golden, load_golden


def _app(**kwargs):
    """带建表 sqlite 内存库的 app（鉴权默认开启，与 Java 默认一致）。"""
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    return TestClient(create_app(stress_repo=StressRepository(factory),
                                 source_repo=SourceRepository(factory),
                                 collector_repo=CollectorRepository(factory), **kwargs))


@pytest.fixture()
def client() -> TestClient:
    # 默认配置（admin/admin123，鉴权开）——与 Java AuthProperties 默认值一致
    return _app()


def test_me_contract_matches_golden(client):
    """/api/auth/me：ApiResponse 包装 + {username, authEnabled} 结构对照 golden。"""
    resp = client.get("/api/auth/me")
    assert_matches_golden(resp.json(), load_golden("auth_me"), resp.status_code)
    assert resp.json()["data"]["authEnabled"] is True


def test_login_success_wire_format(client):
    """登录成功：{token, username, expireHours}；token 可被 JwtService 校验回 sub。"""
    body = client.post("/api/auth/login",
                       json={"username": "admin", "password": "admin123"}).json()
    assert body["code"] == 0
    data = body["data"]
    assert set(data) == {"token", "username", "expireHours"}
    assert data["username"] == "admin" and data["expireHours"] == 12
    assert JwtService(AuthProperties()).verify(data["token"]) == "admin"


def test_login_failure_messages_match_java(client):
    """登录失败：401 业务码 + 固定 message（不区分账号/密码错误防枚举）。"""
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    assert resp.json() == {"code": 401, "message": "账号或密码错误", "data": None}
    resp = client.post("/api/auth/login", json={"username": "", "password": ""})
    assert resp.json() == {"code": 401, "message": "请输入账号与密码", "data": None}
    resp = client.post("/api/auth/login", json={})
    assert resp.json()["code"] == 401


def test_jwt_guard_401_wire_format(client):
    """JWT 拦截：未携带/非法/过期 token → HTTP 401 + 固定体（对齐拦截器字面量）。"""
    resp = client.get("/api/stress/tasks")
    assert resp.status_code == 401
    assert resp.json() == {"code": 401, "message": "未登录或登录已过期", "data": None}

    headers = {"Authorization": "Bearer not-a-jwt"}
    assert client.get("/api/stress/tasks", headers=headers).status_code == 401

    # 有效 token 放行
    token = client.post("/api/auth/login",
                        json={"username": "admin", "password": "admin123"}).json()["data"]["token"]
    resp = client.get("/api/stress/tasks", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200 and resp.json()["code"] == 0


def test_jwt_whitelist_auth_and_agent(client):
    """白名单：/api/auth/**（登录本身）与 /api/agent/**（探针通道）免 JWT。"""
    assert client.get("/api/auth/me").status_code == 200
    resp = client.post("/api/agent/report", json={"type": "jvm", "appName": "ghost"})
    assert resp.status_code == 200, "agent 通道免 JWT（独立令牌把关），项目准入 404"
    assert resp.json()["code"] == 404


def test_ci_api_token_parallel_bypass():
    """X-Perf-Api-Token 与 JWT 并行生效：命中即放行（供 CI 流水线调用）。"""
    c = _app(auth=AuthProperties(enabled=True, api_token="ci-token"))
    assert c.get("/api/stress/tasks",
                 headers={"X-Perf-Api-Token": "ci-token"}).json()["code"] == 0
    assert c.get("/api/stress/tasks").status_code == 401


def test_auth_disabled_allows_anonymous():
    """鉴权关闭（perf.auth.enabled=false）：/api/** 直接放行（本地调试）。"""
    c = _app(auth=AuthProperties(enabled=False))
    assert c.get("/api/stress/tasks").status_code == 200


def test_short_secret_rejected_on_issue():
    """密钥不足 32 字节：签发即失败（对齐 Java HMAC 规范校验）。"""
    svc = JwtService(AuthProperties(enabled=False, secret="short"))
    with pytest.raises(ValueError, match="32"):
        svc.issue("admin")


def test_alg_selection_matches_jjwt_key_size_behavior():
    """算法按密钥长度自动升级（jjwt 0.12 signWith(SecretKey) 行为，双跑实测发现：
    默认 59 字节密钥 Java 实际签发 HS384，Java 文档注释 HS256 与实际不符）。"""
    import time

    import jwt as pyjwt

    from server.auth_service import JwtService as Svc

    assert Svc(AuthProperties(enabled=False))._alg() == "HS384", "默认密钥 59 字节 → HS384"
    s512 = Svc(AuthProperties(enabled=False, secret="x" * 64))
    assert s512._alg() == "HS512"
    s256 = Svc(AuthProperties(enabled=False, secret="x" * 32))
    assert s256._alg() == "HS256"
    # Java 侧 HS384 token（任意 HMAC 家族）可被 Python 侧校验
    props = AuthProperties(enabled=False)
    t384 = pyjwt.encode({"sub": "admin", "iat": int(time.time()),
                         "exp": int(time.time()) + 3600},
                        props.secret.encode(), algorithm="HS384")
    assert JwtService(props).verify(t384) == "admin"


def test_expired_token_rejected():
    """过期 token：verify 返回 None（按未登录处理，不抛异常）。"""
    import time
    props = AuthProperties(enabled=False)
    now = int(time.time())
    import jwt as pyjwt
    expired = pyjwt.encode({"sub": "admin", "iat": now - 7200, "exp": now - 3600},
                           props.secret.encode(), algorithm="HS256")
    assert JwtService(props).verify(expired) is None
