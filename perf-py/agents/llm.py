"""LLM 客户端抽象：OpenAI 兼容 API（openai-python 懒加载）+ 测试替身契约。

Java 侧既有 AI 对话即走 OpenAI 兼容 API（语义不变）；本层为 Agent 编排提供
最小 chat 接口——不绑定 SDK 细节，测试可注入 FakeLlmClient 无网络运行。
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str = ""
    name: str = ""
    arguments: dict = field(default_factory=dict)


@dataclass
class ChatResult:
    """单轮对话结果：assistant 消息（openai messages 兼容形态）+ 解析后的工具调用。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    def assistant_message(self) -> dict:
        """转 messages 数组元素（工具调用需原样回传以配对 tool 结果）。"""
        msg: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name,
                                 "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                }
                for c in self.tool_calls
            ]
        return msg


class LlmClient(ABC):
    """最小 chat 契约：messages + tools → ChatResult。

    可选流式能力：实现 chat_stream(messages, tools) 生成器——逐个 yield 正文
    delta（str），经 StopIteration.value 返回完整 ChatResult（含聚合 tool_calls）。
    SSE 服务侧以 hasattr 探测该能力，探测失败自动回退非流式。
    """

    @abstractmethod
    def chat(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        """发起一轮补全；tools 为 openai function calling schema 数组（可为空）。"""


class OpenAiCompatClient(LlmClient):
    """OpenAI 兼容 API 客户端（openai-python 懒加载；base_url 可指向任何兼容网关）。"""

    def __init__(self, model: str, base_url: str | None = None,
                 api_key: str | None = None, temperature: float = 0.2) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.temperature = temperature
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "openai 未安装：pip install -e 'perf-py[agents]'") from exc
            self._client = OpenAI(
                base_url=self.base_url or None,
                api_key=self.api_key or "not-set",
            )
        return self._client

    def _params(self, messages: list[dict], tools: list[dict], stream: bool) -> dict:
        params: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            params["tools"] = tools
        if stream:
            params["stream"] = True
        return params

    def chat(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        client = self._ensure_client()
        resp = client.chat.completions.create(**self._params(messages, tools, False))
        msg = resp.choices[0].message
        result = ChatResult(content=msg.content)
        for c in (msg.tool_calls or []):
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result.tool_calls.append(ToolCall(id=c.id, name=c.function.name, arguments=args))
        return result

    def chat_stream(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        """流式补全（生成器）：yield 正文 delta，StopIteration.value 返回 ChatResult。

        工具调用轮：无正文 delta，仅返回聚合后的 tool_calls（对齐 OpenAI 流式
        语义：tool_call 分片到达时 content 为空）。
        """
        client = self._ensure_client()
        stream = client.chat.completions.create(**self._params(messages, tools, True))
        deltas: list[str] = []
        calls: dict[str, dict] = {}
        result = ChatResult()
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                deltas.append(delta.content)
                yield delta.content
            for c in (delta.tool_calls or []):
                agg = calls.setdefault(c.index, {"id": "", "name": "", "arguments": ""})
                if c.id:
                    agg["id"] = c.id
                if c.function and c.function.name:
                    agg["name"] = c.function.name
                if c.function and c.function.arguments:
                    agg["arguments"] += c.function.arguments
        for idx in sorted(calls):
            agg = calls[idx]
            try:
                args = json.loads(agg["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            result.tool_calls.append(ToolCall(id=agg["id"], name=agg["name"], arguments=args))
        result.content = "".join(deltas) if deltas else None
        return result


def client_from_env() -> OpenAiCompatClient | None:
    """从环境变量构建客户端：PERF_LLM_MODEL（必需）/ PERF_LLM_BASE_URL / PERF_LLM_API_KEY。"""
    model = os.environ.get("PERF_LLM_MODEL")
    if not model:
        return None
    return OpenAiCompatClient(
        model=model,
        base_url=os.environ.get("PERF_LLM_BASE_URL"),
        api_key=os.environ.get("PERF_LLM_API_KEY"),
    )
