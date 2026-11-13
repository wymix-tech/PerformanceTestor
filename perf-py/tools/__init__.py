"""AI Agent 工具集（阶段 4 · 目的 2 核心）。

将阶段 2/3 产出的能力包装为 Agent 可调用的工具，双形态暴露：
- OpenAI Function Calling schema（openai_tools_schema）
- MCP server stdio 形态（perf_cli/mcp_server.py，`perf mcp` 启动）

设计约定：handler 接收 dict 参数、返回 JSON 可序列化 dict；
异常一律在 dispatch 层捕获转为 {"error": ...}，绝不向 Agent 抛栈。
"""

from __future__ import annotations

from tools.capacity_tools import CAPACITY_TOOLS
from tools.datagen_tools import DATAGEN_TOOLS
from tools.registry import Tool, build_registry, call_tool, openai_tools_schema
from tools.slowlog_tools import SLOWLOG_TOOLS
from tools.source_tools import SOURCE_TOOLS
from tools.stress_tools import STRESS_TOOLS

__all__ = [
    "Tool",
    "build_registry",
    "call_tool",
    "openai_tools_schema",
    "CAPACITY_TOOLS",
    "DATAGEN_TOOLS",
    "SLOWLOG_TOOLS",
    "SOURCE_TOOLS",
    "STRESS_TOOLS",
]
