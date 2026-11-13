"""AI Agent 编排层（阶段 4）：ReAct 单体 + 三角色流水线原型。"""

from __future__ import annotations

from agents.llm import ChatResult, LlmClient, OpenAiCompatClient, ToolCall, client_from_env
from agents.pipeline import run_pipeline
from agents.react import DEFAULT_MAX_STEPS, Agent, AgentReport
from agents.roles import ROLE_ADVISORY, ROLE_ANALYSIS, ROLE_STRESS, ROLES, RoleDef

__all__ = [
    "ChatResult", "LlmClient", "OpenAiCompatClient", "ToolCall", "client_from_env",
    "DEFAULT_MAX_STEPS", "Agent", "AgentReport",
    "ROLES", "RoleDef", "ROLE_STRESS", "ROLE_ANALYSIS", "ROLE_ADVISORY",
    "run_pipeline",
]
