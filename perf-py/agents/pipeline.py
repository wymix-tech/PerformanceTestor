"""多 Agent 编排原型：压测 → 分析 → 修复建议，串行流水线（Issue #23 阶段 4）。

编排原则：角色间只传结构化上下文（AgentReport.context_block），不共享内存态；
单角色失败（max_steps 截断）不阻断后续角色——下游基于已有观察降级推理。
"""

from __future__ import annotations

from agents.llm import LlmClient
from agents.react import DEFAULT_MAX_STEPS, Agent, AgentReport
from agents.roles import ROLES
from tools.registry import build_registry


def run_pipeline(task: str, llm: LlmClient, registry=None,
                 max_steps_per_agent: int = DEFAULT_MAX_STEPS) -> dict:
    """三角色串行编排：返回各角色报告与最终修复建议。"""
    registry = registry if registry is not None else build_registry()
    reports: list[AgentReport] = []
    context = ""
    for role in ROLES:
        agent = Agent(name=role.name, system_prompt=role.prompt(), llm=llm,
                      registry=registry, tool_names=list(role.tool_names),
                      max_steps=max_steps_per_agent)
        report = agent.run(task, context=context)
        reports.append(report)
        context = report.context_block()
    return {
        "task": task,
        "reports": [r.to_dict() for r in reports],
        "finalAdvice": reports[-1].final_answer,
        "degraded": any(r.stopped_reason == "max_steps" for r in reports),
    }
