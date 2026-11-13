"""MCP server stdio 形态：同一 registry 的第二暴露形态（阶段 4 · 目的 2）。

协议：newline-delimited JSON-RPC 2.0（MCP stdio 传输），仅实现工具相关最小面：
- initialize → protocolVersion + capabilities + serverInfo
- tools/list → {tools: [{name, description, inputSchema}]}
- tools/call → {content: [{type: "text", text: <JSON 字符串>}], isError}
- ping 兜底

零第三方依赖（无 mcp SDK）：宿主（IDE / Agent 运行时）以
`perf mcp` 为 stdio 子进程接入即可。通知类请求（无 id）不回包。
"""

from __future__ import annotations

import json
import sys

from tools.registry import Tool, build_registry, call_tool

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "perf-py"


def _tool_entry(t: Tool) -> dict:
    entry = {
        "name": t.name,
        "description": t.description,
        "inputSchema": t.input_schema(),
    }
    if not t.read_only:
        entry["annotations"] = {"readOnlyHint": False}
    return entry


def handle_request(registry: dict[str, Tool], method: str, params: dict) -> tuple[dict, bool]:
    """处理单请求 → (result, is_error)；未知方法抛 ValueError 由调用方转 -32601。"""
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": _version()},
        }, False
    if method == "tools/list":
        return {"tools": [_tool_entry(registry[k]) for k in sorted(registry)]}, False
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        result = call_tool(registry, name, args)
        is_error = "error" in result
        return {
            "content": [{"type": "text",
                         "text": json.dumps(result, ensure_ascii=False)}],
            "isError": is_error,
        }, False
    if method == "ping":
        return {}, False
    raise ValueError(f"方法不支持: {method}")


def serve(registry: dict[str, Tool] | None = None,
          stdin=None, stdout=None) -> None:
    """stdio 主循环：逐行读 JSON-RPC，写单行响应；EOF 退出。"""
    registry = registry if registry is not None else build_registry()
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        resp = _process_line(registry, line)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


def _process_line(registry: dict[str, Tool], line: str) -> dict | None:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        return _error_response(None, -32700, f"JSON 解析失败: {e}")
    req_id = msg.get("id")
    method = msg.get("method", "")
    if req_id is None:
        return None  # 通知（如 notifications/initialized）：不回包
    try:
        result, _is_error = handle_request(registry, method, msg.get("params") or {})
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    except ValueError as e:
        return _error_response(req_id, -32601, str(e))
    except Exception as e:  # noqa: BLE001 服务器兜底
        return _error_response(req_id, -32603, f"{type(e).__name__}: {e}")


def _error_response(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _version() -> str:
    from perf_cli import __version__
    return __version__
