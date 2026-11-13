"""SSE 流式事件契约：逐项对齐 Java AiStreamEventType / AiStreamEvent。

帧格式（与 Spring SseEmitter 下发一致，前端用 fetch + ReadableStream 读取）：
    event: <type>\n
    data: <payload 的 JSON 编码字符串>\n\n

payload 统一为字符串：token/thought 为纯文本，tool/observation/done 为 JSON 文本；
data 侧再做一次 JSON 编码，避免正文换行破坏 SSE 帧格式。
"""

from __future__ import annotations

import json

TOKEN = "token"
THOUGHT = "thought"
TOOL = "tool"
OBSERVATION = "observation"
DONE = "done"
ERROR = "error"

EVENT_NAMES = (TOKEN, THOUGHT, TOOL, OBSERVATION, DONE, ERROR)


def sse_frame(event_name: str, payload: str) -> str:
    """构造单帧：data 为 payload 的 JSON 编码字符串（转义换行）。"""
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def token(text: str) -> str:
    return sse_frame(TOKEN, text)


def thought(text: str) -> str:
    return sse_frame(THOUGHT, text)


def tool(payload: dict) -> str:
    return sse_frame(TOOL, json.dumps(payload, ensure_ascii=False))


def observation(payload: dict) -> str:
    return sse_frame(OBSERVATION, json.dumps(payload, ensure_ascii=False))


def done(payload: dict) -> str:
    return sse_frame(DONE, json.dumps(payload, ensure_ascii=False))


def error(message: str) -> str:
    return sse_frame(ERROR, message)
