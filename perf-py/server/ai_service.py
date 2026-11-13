"""AI Agent 服务：会话管理 + 角色路由 + ReAct 流式循环（对齐 Java AiAgentService）。

契约对齐点：
- 会话：内存 dict（sessionId → 消息历史 + 角色），会话级串行锁防双通道并发互踩
- 角色：GENERAL/STRESS_EXECUTOR/ROOT_CAUSE_ANALYST/FIX_ADVISOR，关键词检测对齐
  PromptBuilder.detectRole 的意图分档顺序（显式闭环 > 强修复 > 归因 > 压测 > 弱修复）
- 循环：MAX_ROUNDS=10 轮上限；工具观察截断 MAX_OBSERVATION_CHARS=2000 回填；
  轮次耗尽给基于已有证据的阶段性结论
- 事件：thought（轮次心跳）/ tool / observation（截断预览）/ token / done（mode/role/rounds）
- 沉淀：结论 ≥ MIN_KNOWLEDGE_CHARS(40) 入知识库，tags = role + 已用工具
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable

from agents.llm import LlmClient
from server import sse
from server.knowledge import KnowledgeService

MAX_ROUNDS = 10
MAX_OBSERVATION_CHARS = 2000
MIN_KNOWLEDGE_CHARS = 40

# 角色名（对齐 Java AiRole 枚举 name()，响应中透传）
GENERAL = "GENERAL"
STRESS_EXECUTOR = "STRESS_EXECUTOR"
ROOT_CAUSE_ANALYST = "ROOT_CAUSE_ANALYST"
FIX_ADVISOR = "FIX_ADVISOR"

_ROLE_LABELS = {
    GENERAL: "通用助手",
    STRESS_EXECUTOR: "压测执行",
    ROOT_CAUSE_ANALYST: "根因分析",
    FIX_ADVISOR: "修复建议",
}

# 关键词分档顺序对齐 PromptBuilder.detectRole
_ROLE_KEYWORDS = [
    (STRESS_EXECUTOR, ("自动执行", "一键检测", "一键", "完整闭环", "闭环", "快速开始")),
    (FIX_ADVISOR, ("提示词", "怎么改", "如何改", "如何优化", "整改", "fix", "patch")),
    (ROOT_CAUSE_ANALYST, ("根因", "为什么", "原因", "瓶颈", "诊断", "排查", "分析")),
    (STRESS_EXECUTOR, ("压测", "并发", "容量", "扩容", "灌数", "灌注", "灌入", "造数",
                       "qps", "吞吐", "基准", "报告")),
    (FIX_ADVISOR, ("修复", "方案", "优化")),
    # 观测类问题描述：默认按根因分析处理（对齐 Java 第 6 档）
    (ROOT_CAUSE_ANALYST, ("慢", "调用链", "热点", "gc", "内存", "线程", "超时")),
]

_BASE_PROMPT = (
    "你是 PerformanceTestor 平台的性能检测助手，负责帮助用户定位 Java 应用性能问题并给出修复方案。\n"
    "推理格式（ReAct）：每一轮先给一句话 Thought（要做什么、为什么），随后调用工具获取证据；"
    "证据足够时不再调用工具，直接给出结论。工具返回空 = 暂无数据，如实说明，不编造数据。"
)


class AiSession:
    """会话状态：消息历史 + 角色（同一 sessionId 共享，需串行访问）。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.role: str = GENERAL
        self.lock = threading.Lock()


def detect_role(message: str | None) -> str:
    """意图路由（分档顺序与关键词对齐 Java PromptBuilder.detectRole）。"""
    m = (message or "").lower()
    if not m.strip():
        return GENERAL
    for role, keywords in _ROLE_KEYWORDS:
        if any(k.lower() in m for k in keywords):
            return role
    return GENERAL


def system_prompt_for(role: str, tool_names: list[str]) -> str:
    caps = "、".join(tool_names) if tool_names else "暂无工具"
    return (f"{_BASE_PROMPT}\n\n当前角色：{_ROLE_LABELS.get(role, role)}。\n"
            f"可用工具：{caps}。")


def truncate(text: str | None, max_chars: int) -> str:
    if text is None:
        return ""
    return text if len(text) <= max_chars else text[:max_chars]


class AiAgentService:
    """对话服务：同步 chat 与流式 chat_stream 双通道，共享会话历史。"""

    def __init__(self, llm: LlmClient, registry, knowledge: KnowledgeService | None = None,
                 max_rounds: int = MAX_ROUNDS) -> None:
        self.llm = llm
        self.registry = registry
        self.knowledge = knowledge
        self.max_rounds = max_rounds
        self.sessions: dict[str, AiSession] = {}
        self._sessions_lock = threading.Lock()

    # ===== 会话管理 =====

    def clear(self, session_id: str) -> None:
        with self._sessions_lock:
            self.sessions.pop(session_id or "default", None)

    def _session(self, session_id: str) -> AiSession:
        sid = session_id if session_id and session_id.strip() else "default"
        with self._sessions_lock:
            session = self.sessions.get(sid)
            if session is None:
                session = AiSession()
                self.sessions[sid] = session
            return session

    # ===== 对话入口 =====

    def chat(self, session_id: str, message: str, app_name: str | None = None,
             sink: Callable[[str], None] | None = None) -> dict:
        """同步对话（sink 可空）。返回 {reply, toolCalls, mode, role, rounds, steps}。"""
        session = self._session(session_id)
        with session.lock:
            return self._do_chat(session, message, app_name, sink, stream=False)

    def chat_stream(self, session_id: str, message: str, app_name: str | None = None,
                    sink: Callable[[str], None] | None = None) -> dict:
        """流式对话（事件经 sink 推送 SSE 帧；契约同 Java /api/ai/chat/stream）。"""
        session = self._session(session_id)
        with session.lock:
            return self._do_chat(session, message, app_name, sink, stream=True)

    # ===== 单轮实现 =====

    def _do_chat(self, session: AiSession, message: str, app_name: str | None,
                 sink: Callable[[str], None] | None, stream: bool) -> dict:
        emit = sink or (lambda _frame: None)
        role = detect_role(message)
        session.role = role

        tools_schema = [self.registry[k].openai_schema() for k in sorted(self.registry)]
        tool_names = sorted(self.registry)
        messages = list(session.messages)
        _apply_system_prompt(messages, system_prompt_for(role, tool_names))

        messages.append({"role": "user", "content": message})

        mode = "llm"
        rounds = 0
        reply = ""
        tool_log: list[dict] = []
        steps: list[dict] = []
        used_tools: set[str] = set()

        while rounds < self.max_rounds:
            rounds += 1
            if self.llm is None:
                # LLM 未配置：规则兜底（对齐 Java mode="rule" 分支，不 500）
                mode = "rule"
                reply = ("LLM 未配置（需设置 PERF_LLM_MODEL / PERF_LLM_BASE_URL / "
                         "PERF_LLM_API_KEY）：当前为规则兜底模式，无法进行推理对话。")
                messages.append({"role": "assistant", "content": reply})
                emit(sse.token(reply))
                break
            # 轮次心跳：工具执行期间无正文，防止代理层断连（对齐 Java thought 语义）
            emit(sse.thought(f"第 {rounds} 轮推理中…"))
            stream_fn = getattr(self.llm, "chat_stream", None) if stream else None
            if stream_fn is not None:
                result = self._chat_stream_round(stream_fn, messages, tools_schema, emit)
                streamed = True
            else:
                result = self.llm.chat(messages, tools_schema)
                streamed = False
            calls = result.tool_calls or []
            if not calls:
                reply = result.content or ""
                messages.append({"role": "assistant", "content": reply})
                if not streamed and reply:
                    emit(sse.token(reply))  # 非流式路径整段下发
                break
            messages.append(result.assistant_message())
            for call in calls:
                tool_log.append({"name": call.name, "arguments": call.arguments})
                emit(sse.tool({"name": call.name, "arguments": call.arguments}))
                from tools.registry import call_tool

                raw = call_tool(self.registry, call.name, call.arguments)
                observation_text = json.dumps(raw, ensure_ascii=False)
                used_tools.add(call.name)
                steps.append({"action": f"{call.name}({call.arguments})",
                              "observation": truncate(observation_text, MAX_OBSERVATION_CHARS)})
                emit(sse.observation({"name": call.name,
                                      "preview": truncate(observation_text, 200)}))
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": truncate(observation_text, MAX_OBSERVATION_CHARS),
                })
        else:
            mode = "llm"
            reply = self._summarize(steps)
            messages.append({"role": "assistant", "content": reply})
            emit(sse.token(reply))

        # 会话历史收敛：保留 system + 最近消息（对齐 Java 会话内累积）
        session.messages = messages

        # 结论沉淀（对齐 Java saveKnowledge：正文足够长才入库；
        # rule 兜底文案非分析结论，不入库避免污染检索）
        if (self.knowledge is not None and mode == "llm"
                and reply and len(reply.strip()) >= MIN_KNOWLEDGE_CHARS):
            tags = role.lower() + ("," + ",".join(sorted(used_tools)) if used_tools else "")
            self.knowledge.save(app_name, _first_line(reply), reply, tags)

        out = {
            "reply": reply,
            "toolCalls": tool_log,
            "mode": mode,
            "role": role,
            "rounds": rounds,
            "steps": steps,
        }
        emit(sse.done({"mode": mode, "role": role, "rounds": rounds}))
        return out

    def _chat_stream_round(self, stream_fn, messages: list[dict], tools_schema,
                           emit) -> object:
        """流式轮次：逐 delta 下发 token（仅正文轮有增量），
        经 StopIteration.value 取回完整 ChatResult（含聚合 tool_calls）。
        """
        gen = stream_fn(messages, tools_schema)
        try:
            while True:
                delta = next(gen)
                if delta:
                    emit(sse.token(delta))
        except StopIteration as stop:
            return stop.value

    def _summarize(self, steps: list[dict]) -> str:
        if not steps:
            return "本轮未获取到有效工具证据，请缩小问题范围后重试。"
        lines = [f"（已达最大推理轮次 {self.max_rounds} 轮，"
                 "以下基于已获取的证据给出阶段性结论）"]
        for i, s in enumerate(steps, 1):
            lines.append(f"{i}. {s['action']} → {s['observation'][:200]}")
        return "\n".join(lines)


def _apply_system_prompt(messages: list[dict], prompt: str) -> None:
    """系统提示词就位：已存在则原位更新，否则插入队首（对齐 Java applySystemPrompt）。"""
    if messages and messages[0].get("role") == "system":
        messages[0] = {"role": "system", "content": prompt}
    else:
        messages.insert(0, {"role": "system", "content": prompt})


def _first_line(content: str) -> str:
    nl = content.find("\n")
    return (content[:nl] if nl > 0 else content).strip()
