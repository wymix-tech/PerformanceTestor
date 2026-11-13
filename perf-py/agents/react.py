"""Agent 单体：ReAct 循环（思考 → 工具调用 → 观察 → ... → 最终回答）。

循环即 orchestration 的最小单元：模型每轮可发起多个工具调用，registry
统一分发（异常转 {"error": ...}）；达到 max_steps 强制截断，绝不静默死循环。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agents.llm import LlmClient
from tools.registry import Tool, call_tool

DEFAULT_MAX_STEPS = 12


@dataclass
class AgentReport:
    """单 Agent 执行报告：步骤流水 + 最终回答（供编排器向下传递）。"""

    agent: str
    task: str
    final_answer: str | None = None
    steps: list[dict] = field(default_factory=list)
    stopped_reason: str = "final"  # final | max_steps

    def context_block(self) -> str:
        """序列化为下一角色的上下文：结论 + 工具观察（JSON 行）。"""
        lines = [f"[{self.agent} Agent 结论] {self.final_answer or '（未得出结论）'}"]
        if self.steps:
            lines.append("[工具观察]")
            for s in self.steps:
                lines.append(json.dumps(s, ensure_ascii=False))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "task": self.task,
            "finalAnswer": self.final_answer,
            "steps": self.steps,
            "stoppedReason": self.stopped_reason,
        }


class Agent:
    """ReAct 循环执行器（一个角色一个实例，注册表可按角色裁剪）。"""

    def __init__(self, name: str, system_prompt: str, llm: LlmClient,
                 registry: dict[str, Tool], tool_names: list[str] | None = None,
                 max_steps: int = DEFAULT_MAX_STEPS) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.llm = llm
        self.max_steps = max_steps
        if tool_names is None:
            self.registry = dict(registry)
        else:
            missing = [t for t in tool_names if t not in registry]
            if missing:
                raise ValueError(f"角色 {name} 引用了未注册工具: {missing}")
            self.registry = {t: registry[t] for t in tool_names}

    def run(self, task: str, context: str = "") -> AgentReport:
        report = AgentReport(agent=self.name, task=task)
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task if not context else f"{task}\n\n[上游上下文]\n{context}"},
        ]
        tools_schema = [self.registry[k].openai_schema() for k in sorted(self.registry)]

        for _ in range(self.max_steps):
            resp = self.llm.chat(messages, tools_schema)
            if not resp.tool_calls:
                messages.append(resp.assistant_message())
                report.final_answer = resp.content
                report.stopped_reason = "final"
                return report
            messages.append(resp.assistant_message())
            for call in resp.tool_calls:
                result = call_tool(self.registry, call.name, call.arguments)
                report.steps.append({
                    "tool": call.name,
                    "arguments": call.arguments,
                    "result": result,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

        report.stopped_reason = "max_steps"
        return report
