"""jmx_import（JMeter 脚本导入）解析与端点测试。

覆盖：生成侧 JMX 往返解析（generate_jmx → parse_jmx）、真实形态的
多采样器/HeaderManager/GET query 参数/JSONPostProcessor/降级 warning、
ThreadGroup 负载预填、非法文件拒绝、multipart 端点 wire。
"""

import pytest
from fastapi.testclient import TestClient

from engine.jmeter_engine import generate_jmx
from scenario.model import LoadSpec, RequestSpec, Scenario
from server.app import create_app
from server.jmx_import import parse_jmx
from server.stress_repo import StressRepository

# ---------------------------------------------------------------- 单接口往返


def _scenario() -> Scenario:
    return Scenario(
        name="login-plan", app="demo-app",
        request=RequestSpec(url="http://127.0.0.1:8081/login", method="POST",
                            headers={"Content-Type": "application/json",
                                     "X-Tenant-Id": "1"},
                            body='{"user": "u"}',
                            assertions=[
                                {"source": "status", "op": "eq", "expected": "200"},
                                {"source": "body", "op": "contains", "expected": "ok"},
                            ]),
        load=LoadSpec(concurrency=8, duration_sec=60, warmup_sec=5, ramp_up_sec=5),
    )


def test_roundtrip_generated_jmx():
    """generate_jmx 生成 → parse_jmx 解析：方法/URL/body/断言/负载逐项还原。"""
    out = parse_jmx(generate_jmx(_scenario()))
    assert out["warnings"] == []
    assert len(out["steps"]) == 1
    step = out["steps"][0]
    assert step["name"] == "http://127.0.0.1:8081/login"
    assert step["method"] == "POST"
    assert step["url"] == "http://127.0.0.1:8081/login"
    assert step["body"] == '{"user": "u"}'
    # generate_jmx 生成侧不写 HeaderManager（既有局限），headersText 为空；
    # 导入能力面向外部真实 JMX（HeaderManager 解析见 rich 用例）
    assert step["headersText"] == ""
    assert step["assertText"].splitlines() == ["status eq 200", "body contains ok"]
    assert out["load"] == {"concurrency": 8, "durationSec": 65, "rampUpSec": 5}


# ---------------------------------------------------------------- 真实形态 JMX


def _rich_jmx() -> str:
    """两个采样器 + HeaderManager + GET query 参数 + JSONPostProcessor + 降级项。"""
    return """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6">
  <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="demo-plan">
    <ThreadGroup testname="threads">
      <stringProp name="ThreadGroup.num_threads">20</stringProp>
      <stringProp name="ThreadGroup.ramp_time">10</stringProp>
      <stringProp name="ThreadGroup.duration">120</stringProp>
    </ThreadGroup>
    <ThreadGroup testname="threads2"><stringProp name="ThreadGroup.num_threads">9</stringProp></ThreadGroup>
    <HTTPSamplerProxy testname="登录">
      <stringProp name="HTTPSampler.protocol">https</stringProp>
      <stringProp name="HTTPSampler.domain">demo.example.com</stringProp>
      <stringProp name="HTTPSampler.port">8443</stringProp>
      <stringProp name="HTTPSampler.path">/api/login</stringProp>
      <stringProp name="HTTPSampler.method">POST</stringProp>
      <stringProp name="HTTPSampler.postBodyRaw">true</stringProp>
      <elementProp name="HTTPsampler.Arguments" elementType="Arguments">
        <elementProp elementType="Argument">
          <stringProp name="Argument.value">{"user":"u"}</stringProp>
        </elementProp>
      </elementProp>
      <elementProp name="HM-1" elementType="HeaderManager">
        <collectionProp name="HeaderManager.headers">
          <elementProp>
            <stringProp name="Header.name">Authorization</stringProp>
            <stringProp name="Header.value">Bearer xxx</stringProp>
          </elementProp>
        </collectionProp>
      </elementProp>
      <JSONPostProcessor testname="jp">
        <stringProp name="JSONPostProcessor.referenceName">token</stringProp>
        <collectionProp name="JSONPostProcessor.jsonPathQueryStrings">
          <stringProp name="">$.data.token</stringProp>
        </collectionProp>
      </JSONPostProcessor>
    </HTTPSamplerProxy>
    <HTTPSamplerProxy testname="查列表">
      <stringProp name="HTTPSampler.domain">demo.example.com</stringProp>
      <stringProp name="HTTPSampler.path">/api/list</stringProp>
      <stringProp name="HTTPSampler.method">GET</stringProp>
      <elementProp name="HTTPsampler.Arguments" elementType="Arguments">
        <elementProp elementType="Argument">
          <stringProp name="Argument.name">page</stringProp>
          <stringProp name="Argument.value">1</stringProp>
        </elementProp>
        <elementProp elementType="Argument">
          <stringProp name="Argument.name">size</stringProp>
          <stringProp name="Argument.value">20</stringProp>
        </elementProp>
      </elementProp>
      <RegexExtractor testname="rx">
        <stringProp name="RegexExtractor.refname">oldVar</stringProp>
      </RegexExtractor>
    </HTTPSamplerProxy>
    <HTTPSamplerProxy testname="变量域名">
      <stringProp name="HTTPSampler.domain">${__P(host,localhost)}</stringProp>
      <stringProp name="HTTPSampler.path">/api/x</stringProp>
    </HTTPSamplerProxy>
    <ResponseAssertion testname="assert">
      <stringProp name="Assertion.test_field">Assertion.response_code</stringProp>
      <stringProp name="Assertion.test_type">8</stringProp>
      <collectionProp name="Asserts.test_strings">
        <stringProp name="">200</stringProp>
      </collectionProp>
    </ResponseAssertion>
  </TestPlan>
</jmeterTestPlan>"""


@pytest.fixture()
def rich():
    return parse_jmx(_rich_jmx())


def test_steps_wire(rich):
    steps = rich["steps"]
    assert len(steps) == 3
    login, listing, _var_step = steps
    # 登录步骤：https + 非默认端口 / 请求头 / body / 变量提取
    assert login["url"] == "https://demo.example.com:8443/api/login"
    assert login["headersText"] == "Authorization: Bearer xxx"
    assert login["body"] == '{"user":"u"}'
    assert login["assertText"] == ""
    assert (login["extractVar"], login["extractSource"], login["extractPath"]) \
        == ("token", "body", "$.data.token")
    # 查列表步骤：GET 无 body，query 参数拼回 URL
    assert listing["method"] == "GET"
    assert listing["body"] == ""
    assert listing["url"] == "http://demo.example.com/api/list?page=1&size=20"
    assert listing["weight"] == 1


def test_load_uses_first_thread_group(rich):
    assert rich["load"] == {"concurrency": 20, "durationSec": 120, "rampUpSec": 10}


def test_warnings_for_degraded_items(rich):
    text = "\n".join(rich["warnings"])
    assert "2 个线程组" in text
    assert "域名含 JMeter 变量" in text
    assert "RegexExtractor" in text
    assert "计划级断言" in text, "单采样器以外归属不明 → warning"


def test_plan_level_assertion_attached_when_single_sampler():
    jmx = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6">
  <TestPlan testname="p">
    <HTTPSamplerProxy testname="唯一接口">
      <stringProp name="HTTPSampler.domain">h1</stringProp>
      <stringProp name="HTTPSampler.path">/api/a</stringProp>
    </HTTPSamplerProxy>
    <ResponseAssertion testname="assert">
      <stringProp name="Assertion.test_field">Assertion.response_code</stringProp>
      <stringProp name="Assertion.test_type">8</stringProp>
      <collectionProp name="Asserts.test_strings">
        <stringProp name="">200</stringProp>
      </collectionProp>
    </ResponseAssertion>
  </TestPlan>
</jmeterTestPlan>"""
    out = parse_jmx(jmx)
    assert len(out["steps"]) == 1
    assert "status eq 200" in out["steps"][0]["assertText"]
    assert not any("计划级断言" in w for w in out["warnings"])


# ---------------------------------------------------------------- 非法输入


def test_invalid_inputs():
    with pytest.raises(ValueError, match="不是合法的 XML"):
        parse_jmx("<jmeterTestPlan><unclosed>")
    with pytest.raises(ValueError, match="根元素应为 jmeterTestPlan"):
        parse_jmx("<root/>")
    with pytest.raises(ValueError, match="未在 JMX 中找到 HTTP 采样器"):
        parse_jmx("<jmeterTestPlan><TestPlan/></jmeterTestPlan>")


# ---------------------------------------------------------------- 端点 wire


def test_import_endpoint_rejects_non_jmx_and_parses_ok():
    client = TestClient(create_app(stress_repo=StressRepository(
        __import__("server.db", fromlist=["make_engine"]).make_engine(
            "sqlite:///:memory:")), auth_enabled=False))
    bad = client.post("/api/stress/jmx/import",
                      files={"file": ("a.txt", b"x")}).json()
    assert bad == {"code": 1, "message": "仅支持 .jmx / .xml 文件", "data": None}

    ok = client.post("/api/stress/jmx/import", files={
        "file": ("plan.jmx", generate_jmx(_scenario()).encode("utf-8"))}).json()
    assert ok["code"] == 0
    assert len(ok["data"]["steps"]) == 1
    assert ok["data"]["load"]["concurrency"] == 8

    broken = client.post("/api/stress/jmx/import",
                         files={"file": ("plan.jmx", b"<not-xml")}).json()
    assert broken["code"] == 1
    assert "不是合法的 XML" in broken["message"]


# ---------------------------------------------------------------- 导出端点


def _client():
    return TestClient(create_app(stress_repo=StressRepository(
        __import__("server.db", fromlist=["make_engine"]).make_engine(
            "sqlite:///:memory:")), auth_enabled=False))


def _export_payload() -> dict:
    return {
        "taskName": "导出方案",
        "targetUrl": "http://127.0.0.1:8081/login",
        "method": "POST",
        "concurrency": 8,
        "durationSec": 60,
        "warmupSec": 5,
        "headers": {"Content-Type": "application/json"},
        "body": '{"user": "u"}',
        "assertions": [{"source": "status", "op": "eq", "expected": "200"}],
    }


def test_export_endpoint_single_mode():
    """单接口导出：application/xml 下载 + HeaderManager/断言写入。"""
    client = _client()
    resp = client.post("/api/stress/jmx/export", json=_export_payload())
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert ".jmx" in resp.headers["content-disposition"]
    assert "HTTPSamplerProxy" in resp.text
    assert 'HTTPSampler.method">POST' in resp.text
    assert "Header.name\">Content-Type" in resp.text
    assert "Assertion.response_code" in resp.text
    # 导出的 JMX 可被导入解析器读回（往返闭环）
    out = parse_jmx(resp.text)
    assert len(out["steps"]) == 1
    assert out["steps"][0]["url"] == "http://127.0.0.1:8081/login"
    assert out["load"]["concurrency"] == 8


def test_export_endpoint_scenario_mode_sequential():
    """混合场景导出：步骤按顺序写入、各自携带请求头与断言。"""
    client = _client()
    payload = {
        "taskName": "混合场景",
        "concurrency": 20,
        "durationSec": 60,
        "scenario": [
            {"name": "login", "url": "http://h1:8081/login", "method": "POST",
             "body": '{"u":1}',
             "headers": {"Authorization": "Bearer t"}},
            {"name": "list", "url": "http://h1:8081/list", "method": "GET",
             "assertions": [{"source": "status", "op": "eq", "expected": "200"}]},
        ],
    }
    resp = client.post("/api/stress/jmx/export", json=payload)
    assert resp.status_code == 200
    assert resp.text.count("HTTPSamplerProxy") >= 2
    # 顺序：login 在 list 之前；各步请求头/断言独立
    assert resp.text.index("testname=\"login\"") < resp.text.index("testname=\"list\"")
    assert "Header.name\">Authorization" in resp.text
    assert "Assertion.response_code" in resp.text


def test_export_endpoint_rejects_invalid_payload():
    client = _client()
    # URL 非法（校验层拒绝）→ code=1 JSON 包装
    resp = client.post("/api/stress/jmx/export",
                       json={"targetUrl": "ftp://x", "concurrency": 8, "durationSec": 30})
    body = resp.json()
    assert body["code"] == 1
    assert "http://" in body["message"]
