import pytest

from scenario.loader import ScenarioError, load_scenario
from scenario.model import Scenario


def _write(tmp_path, content: str):
    p = tmp_path / "s.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_single_request_scenario(tmp_path):
    p = _write(
        tmp_path,
        """
name: demo-nplus1
app: demo-app
request:
  url: http://localhost:8081/api/order/list?status=SHIPPED&limit=500
  method: GET
load:
  concurrency: 20
  duration_sec: 30
  warmup_sec: 5
""",
    )
    s = load_scenario(p)
    assert isinstance(s, Scenario)
    assert s.load.concurrency == 20
    payload = s.to_request_payload()
    # camelCase 对齐 StressTaskRequest
    assert "/api/order/list?status=SHIPPED&limit=500" in payload["targetUrl"]
    assert payload["warmupSec"] == 5
    assert payload["durationSec"] == 30
    assert "targetUrl" in payload and "scenario" not in payload


def test_camelcase_keys_accepted(tmp_path):
    p = _write(
        tmp_path,
        """
name: legacy
app: demo-app
request:
  url: http://x/api
load:
  durationSec: 15
  warmupSec: 3
  rampUpSec: 2
  qpsLimit: 100
""",
    )
    s = load_scenario(p)
    assert s.load.duration_sec == 15
    assert s.to_request_payload()["qpsLimit"] == 100


def test_scenario_mode_with_extract_and_assertion(tmp_path):
    p = _write(
        tmp_path,
        """
name: login-flow
app: demo-app
steps:
  - name: login
    url: http://localhost:8081/api/scenario/login
    method: POST
    body: '{"username":"u"}'
    extract:
      - var: token
        source: body
        path: result.token
    assertions:
      - source: status
        op: eq
        expected: "200"
  - name: order
    url: http://localhost:8081/api/scenario/order
    method: POST
    headers:
      Authorization: Bearer {{token}}
    weight: 2
load:
  concurrency: 10
""",
    )
    s = load_scenario(p)
    assert s.is_scenario_mode
    payload = s.to_request_payload()
    steps = payload["scenario"]
    assert steps[0]["extract"][0]["path"] == "result.token"
    assert steps[1]["weight"] == 2
    assert steps[1]["headers"]["Authorization"] == "Bearer {{token}}"


def test_env_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_TOKEN", "tk-abc")
    p = _write(
        tmp_path,
        """
name: envtest
app: demo-app
request:
  url: http://localhost:8081/api/order/recent
  headers:
    X-Perf-Test: ${DEMO_TOKEN}
""",
    )
    s = load_scenario(p)
    assert s.request.headers["X-Perf-Test"] == "tk-abc"


def test_missing_env_collected_as_error(tmp_path, monkeypatch):
    monkeypatch.delenv("DEFINITELY_MISSING_VAR", raising=False)
    p = _write(
        tmp_path,
        """
name: envmiss
app: demo-app
request:
  url: http://localhost:8081/api/order/recent
  headers:
    X-Token: ${DEFINITELY_MISSING_VAR}
""",
    )
    with pytest.raises(ScenarioError) as ei:
        load_scenario(p)
    assert "DEFINITELY_MISSING_VAR" in ei.value.errors[0]


def test_unknown_field_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
name: typo
app: demo-app
reques:
  url: http://x
""",
    )
    with pytest.raises(ScenarioError) as ei:
        load_scenario(p)
    assert any("reques" in e or "request" in e for e in ei.value.errors)


def test_validation_errors_aggregated(tmp_path):
    p = _write(
        tmp_path,
        """
name: bad
app: demo-app
request:
  url: http://x
  method: FETCH
load:
  concurrency: 0
""",
    )
    with pytest.raises(ScenarioError) as ei:
        load_scenario(p)
    joined = "\n".join(ei.value.errors)
    assert "method" in joined or "非法 HTTP 方法" in joined
    assert "concurrency" in joined


def test_ramp_up_must_not_exceed_warmup(tmp_path):
    p = _write(
        tmp_path,
        """
name: badramp
app: demo-app
request:
  url: http://x
load:
  warmup_sec: 5
  ramp_up_sec: 30
""",
    )
    with pytest.raises(ScenarioError):
        load_scenario(p)


def test_stages_payload(tmp_path):
    p = _write(
        tmp_path,
        """
name: capacity
app: demo-app
request:
  url: http://x/api
stages:
  - concurrency: 10
    duration_sec: 30
  - concurrency: 50
    duration_sec: 30
""",
    )
    s = load_scenario(p)
    payload = s.to_request_payload()
    assert payload["stages"] == [{"concurrency": 10, "durationSec": 30}, {"concurrency": 50, "durationSec": 30}]


def test_request_and_steps_mutually_exclusive(tmp_path):
    p = _write(
        tmp_path,
        """
name: both
app: demo-app
request:
  url: http://x
steps:
  - url: http://y
""",
    )
    with pytest.raises(ScenarioError):
        load_scenario(p)
