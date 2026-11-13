"""MCP server 协议行为测试（阶段 4 步骤 1）：initialize / tools/list / tools/call /
通知不回包 / 错误码。
"""

import io
import json

from perf_cli.mcp_server import handle_request, serve
from tools.registry import build_registry


def _msg(**kw) -> str:
    return json.dumps(kw)


def _serve(lines: list[str]) -> list[dict]:
    out = io.StringIO()
    serve(stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_initialize():
    reg = build_registry()
    result, is_error = handle_request(reg, "initialize", {})
    assert not is_error
    assert result["protocolVersion"]
    assert result["capabilities"]["tools"]
    assert result["serverInfo"]["name"] == "perf-py"


def test_tools_list():
    reg = build_registry()
    result, _ = handle_request(reg, "tools/list", {})
    names = [t["name"] for t in result["tools"]]
    assert names == sorted(names) and len(names) == 7
    for t in result["tools"]:
        assert t["inputSchema"]["type"] == "object"
        assert t["inputSchema"]["additionalProperties"] is False
    ro = {t["name"]: t.get("annotations") for t in result["tools"]}
    assert ro["datagen_run"] == {"readOnlyHint": False}
    assert ro["stress_run"] == {"readOnlyHint": False}
    assert ro["capacity_analyze"] is None


def test_tools_call_roundtrip():
    reg = build_registry()
    result, _ = handle_request(reg, "tools/call",
                               {"name": "slowlog_analyze",
                                "arguments": {"content": ""}})
    assert result["isError"] is True, "空内容 → 二选一参数错误"
    payload = json.loads(result["content"][0]["text"])
    assert "path 或 content" in payload["error"]


def test_tools_call_success():
    reg = build_registry()
    log = ("# Time: 2026-11-13T10:00:00\n# User@Host: perf[perf] @  [10.0.0.8]\n"
           "# Query_time: 1.0  Lock_time: 0.0  Rows_sent: 1  Rows_examined: 99\n"
           "SET timestamp=1763008800;\nSELECT 1;\n")
    result, _ = handle_request(reg, "tools/call",
                               {"name": "slowlog_analyze", "arguments": {"content": log}})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["total"] == 1


def test_serve_full_session():
    responses = _serve([
        _msg(jsonrpc="2.0", id=1, method="initialize", params={}),
        _msg(jsonrpc="2.0", method="notifications/initialized"),  # 通知不回包
        _msg(jsonrpc="2.0", id=2, method="tools/list"),
        _msg(jsonrpc="2.0", id=3, method="tools/call",
             params={"name": "capacity_analyze", "arguments": {"target_p95_ms": 100}}),
        _msg(jsonrpc="2.0", id=4, method="no/such/method"),
        "not-json",
        "",
    ])
    assert len(responses) == 5, "通知不回包；坏行回 -32700 错误"
    assert responses[0]["result"]["serverInfo"]["name"] == "perf-py"
    assert len(responses[1]["result"]["tools"]) == 7
    assert responses[2]["result"]["isError"] is True
    assert responses[3]["error"]["code"] == -32601
    assert responses[4]["error"]["code"] == -32700
