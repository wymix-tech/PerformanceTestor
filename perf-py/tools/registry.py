"""工具注册与分发：OpenAI Function Calling schema 导出 + 统一调用入口。

对齐 Java 侧 AI Function Calling 既有形态（LLM tool schema + 调用分发），
本层为语言无关契约：同一 registry 同时服务 openai schema 与 MCP server。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    """单个 Agent 工具：名称 / 描述 / JSON Schema / 处理器。"""

    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], dict]
    # 处理器是否只读（供 MCP annotations 与编排策略参考）
    read_only: bool = True

    def input_schema(self) -> dict:
        """MCP inputSchema 形态（顶层 type=object + additionalProperties=false）。"""
        return {
            "type": "object",
            "properties": self.parameters.get("properties", {}),
            "required": self.parameters.get("required", []),
            "additionalProperties": False,
        }

    def openai_schema(self) -> dict:
        """OpenAI Function Calling 形态。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema(),
            },
        }


def build_registry() -> dict[str, Tool]:
    """聚合全部域工具（新增工具仅需在对应 *_tools.py 追加并在此合并）。

    域工具模块延迟导入：registry 是各域模块的公共依赖，顶层导入会形成环。
    """
    from tools.capacity_tools import CAPACITY_TOOLS
    from tools.datagen_tools import DATAGEN_TOOLS
    from tools.slowlog_tools import SLOWLOG_TOOLS
    from tools.source_tools import SOURCE_TOOLS
    from tools.stress_tools import STRESS_TOOLS

    tools: dict[str, Tool] = {}
    for t in [*STRESS_TOOLS, *CAPACITY_TOOLS, *SLOWLOG_TOOLS,
              *SOURCE_TOOLS, *DATAGEN_TOOLS]:
        if t.name in tools:
            raise ValueError(f"工具名重复: {t.name}")
        tools[t.name] = t
    return tools


def call_tool(registry: dict[str, Tool], name: str, args: dict | None) -> dict:
    """统一分发：未知工具 / 参数缺失 / 处理器异常 → {"error": ...}，绝不抛出。"""
    tool = registry.get(name)
    if tool is None:
        return {"error": f"未知工具: {name}", "available": sorted(registry)}
    try:
        return tool.handler(args or {})
    except Exception as e:  # noqa: BLE001 Agent 边界兜底：失败以 JSON 返回
        return {"error": f"{type(e).__name__}: {e}"}


def openai_tools_schema(registry: dict[str, Tool]) -> list[dict]:
    """OpenAI chat.completions tools 参数形态（按名称稳定排序）。"""
    return [registry[k].openai_schema() for k in sorted(registry)]
