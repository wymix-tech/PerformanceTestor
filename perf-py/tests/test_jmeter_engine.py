import pytest

from engine.jmeter_engine import (
    JMETER_NOT_FOUND,
    JMeterEngine,
    generate_jmx,
    parse_jtl,
    unsupported_assertion_warnings,
)
from scenario.model import AssertionSpec, LoadSpec, RequestSpec, Scenario


def _scenario(assertions: list[AssertionSpec] | None = None) -> Scenario:
    return Scenario(
        name="demo-nplus1",
        app="demo-app",
        request=RequestSpec(
            url="http://localhost:8081/api/user/list?limit=2000",
            method="GET",
            assertions=assertions or [],
        ),
        load=LoadSpec(concurrency=20, duration_sec=30, warmup_sec=5, ramp_up_sec=5, qps_limit=0),
    )


def test_generate_jmx_contains_key_config():
    jmx = generate_jmx(_scenario())
    assert "HTTPSamplerProxy" in jmx
    assert "<stringProp name=\"ThreadGroup.num_threads\">20</stringProp>" in jmx
    assert "<stringProp name=\"ThreadGroup.ramp_time\">5</stringProp>" in jmx
    # duration 覆盖 warmup+duration（JMeter 无预热排除语义，靠 JTL 时间戳过滤）
    assert "<stringProp name=\"ThreadGroup.duration\">35</stringProp>" in jmx
    assert 'HTTPSampler.domain">localhost' in jmx
    assert 'HTTPSampler.port">8081' in jmx
    assert 'HTTPSampler.path">/api/user/list?limit=2000' in jmx
    assert 'HTTPSampler.method">GET' in jmx


def test_generate_jmx_body_and_assertions():
    s = Scenario(
        name="post",
        app="demo-app",
        request=RequestSpec(
            url="http://localhost:8081/api/scenario/login",
            method="POST",
            body='{"username":"u"}',
            assertions=[
                AssertionSpec(source="status", op="eq", expected="200"),
                AssertionSpec(source="body", op="contains", expected="token"),
            ],
        ),
    )
    jmx = generate_jmx(s)
    assert 'HTTPSampler.method">POST' in jmx
    assert 'Argument.value">{"username":"u"}' in jmx
    assert "Assertion.response_code" in jmx
    assert "token" in jmx


def test_generate_jmx_rejects_steps():
    s = Scenario(
        name="steps",
        app="demo-app",
        steps=[__import__("scenario.model", fromlist=["StepSpec"]).StepSpec(url="http://localhost:8081/x")],
    )
    with pytest.raises(ValueError, match="steps"):
        generate_jmx(s)


def test_unsupported_assertion_warnings():
    s = _scenario(
        [
            AssertionSpec(source="status", op="eq", expected="200"),       # 支持
            AssertionSpec(source="json", key="code", op="eq", expected="0"),  # 不支持
            AssertionSpec(source="body", op="regex", expected="id"),       # 支持
            AssertionSpec(source="header", key="X", op="eq", expected="1"),   # 不支持
        ]
    )
    warnings = unsupported_assertion_warnings(s)
    assert len(warnings) == 2
    assert all("不支持" in w for w in warnings)


def test_parse_jtl_filters_dirty_rows(tmp_path):
    p = tmp_path / "r.jtl"
    p.write_text(
        "timeStamp,elapsed,label,responseCode,success,threadName\n"
        "1000,50,api,200,true,t1\n"
        "2000,80,api,500,false,t1\n"
        "bad,row,,,,\n"
        "3000,10,api,200,true,t1\n",
        encoding="utf-8",
    )
    rows = parse_jtl(p)
    assert len(rows) == 3
    assert rows[1]["success"] is False
    assert rows[0]["elapsed"] == 50


def test_engine_missing_binary_raises(tmp_path):
    engine = JMeterEngine(jmeter_bin=None)
    with pytest.raises(FileNotFoundError, match="jmeter"):
        engine.run(_scenario())


def test_engine_missing_binary_message_actionable(tmp_path):
    engine = JMeterEngine(jmeter_bin="/nonexistent/jmeter")
    with pytest.raises(FileNotFoundError) as ei:
        engine.run(_scenario())
    # 报错信息必须可行动：告知安装路径（对齐"错误信息能指导下一步"）
    assert "JMeter" in str(JMETER_NOT_FOUND)
    assert ei.value is not None
