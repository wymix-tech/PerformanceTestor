"""gate_runner / client / verdict 测试（httpx MockTransport，不依赖真实平台）。"""

import json
from pathlib import Path

import httpx
import pytest
import typer
from rich.console import Console

from perf_cli.client import ApiError, PerfClient
from perf_cli.config import load_gate_config, load_server_config
from perf_cli.gate_runner import execute_gate, payload_from_env_gate
from perf_cli.verdict import parse_verdict

VERDICT_PASS = {
    "taskId": 101,
    "taskName": "demo-nplus1",
    "status": "finished",
    "passed": True,
    "noBaseline": False,
    "regressed": False,
    "baselineTaskId": 88,
    "summary": "性能达标",
    "rows": [
        {
            "label": "QPS",
            "base": 100.0,
            "cur": 105.0,
            "deltaPct": 5.0,
            "improved": True,
            "regressed": False,
        },
        {
            "label": "P95(ms)",
            "base": 200.0,
            "cur": 260.0,
            "deltaPct": 30.0,
            "improved": False,
            "regressed": True,
        },
    ],
}

VERDICT_FAIL = {
    "taskId": 102,
    "taskName": "demo-slowsql",
    "status": "finished",
    "passed": False,
    "noBaseline": False,
    "regressed": True,
    "baselineTaskId": 88,
    "summary": "性能回归：与基线任务 #88 相比，P95 上升 30.0%",
    "rows": [],
}


def _transport(gate_data: dict, status_code: int = 200, code: int = 0) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stress/gate":
            assert request.headers["X-Perf-Api-Token"] == "tk-test"
            body = json.loads(request.content.decode())
            assert body["targetUrl"] and "concurrency" in body
            assert request.url.params["failOnNoBaseline"] in ("true", "false")
            return httpx.Response(status_code, json={"code": code, "message": "ok", "data": gate_data})
        if request.url.path == "/api/stress/tasks/101/report":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "task": {"id": 101},
                        "result": {
                            "totalRequests": 5000,
                            "errorRate": 0.01,
                            "qps": 105.0,
                            "p50Ms": 50.0,
                            "p95Ms": 260.0,
                            "p99Ms": 400.0,
                            "avgMs": 60.0,
                            "maxMs": 900.0,
                        },
                    },
                },
            )
        return httpx.Response(404, json={"code": 1, "message": "not found"})

    return httpx.MockTransport(handler)


def _run_gate(tmp_path: Path, scenario_yaml: str | None, gate_data: dict, monkeypatch, **env):
    monkeypatch.setenv("PERF_API_TOKEN", "tk-test")
    monkeypatch.delenv("PERF_SERVER", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    scenario_file = None
    if scenario_yaml:
        scenario_file = tmp_path / "s.yaml"
        scenario_file.write_text(scenario_yaml, encoding="utf-8")
    output = tmp_path / "artifact.json"
    console, err = Console(), Console(stderr=True)
    code = 0
    try:
        execute_gate(
            scenario_file=scenario_file,
            profile=None,
            output=output,
            console=console,
            err_console=err,
            transport=_transport(gate_data),
        )
    except typer.Exit as exc:
        code = exc.exit_code if isinstance(exc.exit_code, int) else 0
    artifact = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
    return code, artifact


SCENARIO = """
name: demo-nplus1
app: demo-app
request:
  url: http://localhost:8081/api/user/list?limit=2000
load:
  concurrency: 20
  duration_sec: 30
  warmup_sec: 5
"""


def test_execute_gate_pass_with_scenario(tmp_path, monkeypatch):
    code, artifact = _run_gate(tmp_path, SCENARIO, VERDICT_PASS, monkeypatch)
    assert code == 0
    assert artifact["verdict"]["passed"] is True
    assert artifact["taskReport"]["result"]["p95Ms"] == 260.0
    assert artifact["scenario"] == "demo-nplus1"
    assert artifact["schema"] == "perf-py.artifact.v1"


def test_execute_gate_fail_exit_1(tmp_path, monkeypatch):
    code, _ = _run_gate(tmp_path, SCENARIO, VERDICT_FAIL, monkeypatch)
    assert code == 1


def test_execute_gate_env_mode(tmp_path, monkeypatch):
    # 无场景文件：走 GATE_* 环境变量（对齐 perf-gate.sh）
    code, artifact = _run_gate(
        tmp_path,
        None,
        VERDICT_PASS,
        monkeypatch,
        GATE_URL="http://localhost:8081/api/order/list?status=SHIPPED&limit=500",
        GATE_CONCURRENCY="10",
        GATE_DURATION="20",
    )
    assert code == 0
    assert artifact["verdict"]["taskId"] == 101


def test_execute_gate_missing_token_exit_2(tmp_path, monkeypatch):
    monkeypatch.delenv("PERF_API_TOKEN", raising=False)
    console, err = Console(), Console(stderr=True)
    with pytest.raises(typer.Exit) as ei:
        execute_gate(
            scenario_file=None,
            profile=None,
            output=None,
            console=console,
            err_console=err,
            transport=_transport(VERDICT_PASS),
        )
    assert ei.value.exit_code == 2


def test_execute_gate_api_error_exit_2(tmp_path, monkeypatch):
    # 业务 code != 0 → 退出码 2（调用异常）
    monkeypatch.setenv("PERF_API_TOKEN", "tk-test")
    console, err = Console(), Console(stderr=True)
    with pytest.raises(typer.Exit) as ei:
        execute_gate(
            scenario_file=None,
            profile=None,
            output=None,
            console=console,
            err_console=err,
            transport=_transport(VERDICT_PASS, code=500),
        )
    assert ei.value.exit_code == 2


def test_client_401_maps_to_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "message": "unauthorized"})

    with PerfClient("http://x", "", transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ApiError) as ei:
            c.run_gate({"targetUrl": "http://t"})
    assert ei.value.status_code == 401
    assert "401" in str(ei.value)


def test_parse_verdict_rows():
    v = parse_verdict(VERDICT_PASS)
    assert v.task_id == 101
    assert v.executed_ok is True
    assert len(v.rows) == 2
    assert v.rows[1].regressed is True
    assert v.rows[1].delta_pct == pytest.approx(30.0)


def test_payload_from_env_gate_matches_perf_gate_sh_body(monkeypatch):
    # 与 scripts/perf-gate.sh 生成的 JSON 字段逐一对应
    monkeypatch.setenv("GATE_URL", "http://t/api")
    monkeypatch.setenv("GATE_APP", "demo-app")
    monkeypatch.setenv("GATE_METHOD", "POST")
    monkeypatch.setenv("GATE_BODY", '{"a":1}')
    monkeypatch.setenv("GATE_CONCURRENCY", "8")
    monkeypatch.setenv("GATE_DURATION", "45")
    monkeypatch.setenv("GATE_WARMUP", "3")
    monkeypatch.setenv("GATE_RAMP_UP", "2")
    monkeypatch.setenv("GATE_QPS_LIMIT", "50")
    payload = payload_from_env_gate(load_gate_config())
    assert payload == {
        "targetUrl": "http://t/api",
        "appName": "demo-app",
        "method": "POST",
        "body": '{"a":1}',
        "concurrency": 8,
        "durationSec": 45,
        "warmupSec": 3,
        "rampUpSec": 2,
        "qpsLimit": 50,
    }


def test_load_server_config_profile_fallback(tmp_path, monkeypatch):
    # env 优先于 profile；profile 文件缺失时报错
    monkeypatch.delenv("PERF_API_TOKEN", raising=False)
    with pytest.raises(FileNotFoundError):
        load_server_config("nonexistent")
