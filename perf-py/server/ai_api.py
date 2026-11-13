"""AI 域 router：/api/ai 契约逐端点对齐 Java（AiController + AiChatStreamController）。

- POST /api/ai/chat            同步对话 → ApiResponse<{reply, toolCalls, mode, role, rounds, steps}>
- POST /api/ai/chat/stream     SSE 流式对话（fetch + ReadableStream 消费，非 EventSource）
- GET  /api/ai/tools           工具清单 [{name, description}]
- GET  /api/ai/knowledge       知识检索（query 空时返回最近结论）
- DELETE /api/ai/knowledge/{id} 删除单条沉淀
- DELETE /api/ai/session/{sid}  清空会话历史

SSE 帧格式与 Spring SseEmitter 一致。流式实现：ReAct 循环在 worker 线程执行，
事件帧经阻塞队列交由 StreamingResponse 生成器逐帧下发（真流式：token 增量与
工具事件实时可见，而非等推理结束一次性回放）。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Generator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from server import sse
from server.knowledge import KnowledgeItem, KnowledgeService

router = APIRouter(prefix="/api/ai")

_STREAM_QUEUE_MAX = 256  # 有界队列：下游断开时不无界堆积


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


@router.post("/chat")
def chat(request: Request, body: dict) -> dict:
    service = request.app.state.ai_service
    result = service.chat(
        (body or {}).get("sessionId") or "default",
        (body or {}).get("message") or "",
        (body or {}).get("appName") or None,
    )
    return _ok(result)


@router.post("/chat/stream")
def chat_stream(request: Request, body: dict) -> StreamingResponse:
    service = request.app.state.ai_service
    session_id = (body or {}).get("sessionId") or "default"
    message = (body or {}).get("message") or ""
    app_name = (body or {}).get("appName") or None

    def generate() -> Generator[str, None, None]:
        frames: queue.Queue = queue.Queue(maxsize=_STREAM_QUEUE_MAX)
        sentinel = object()

        def run() -> None:
            try:
                service.chat_stream(session_id, message, app_name,
                                    sink=frames.put)
            except Exception as e:  # noqa: BLE001 流内兜底：推 error 帧后收尾
                try:
                    frames.put(sse.error(f"{type(e).__name__}: {e}"))
                except queue.Full:
                    pass
            finally:
                try:
                    frames.put(sentinel)
                except queue.Full:
                    pass

        threading.Thread(target=run, daemon=True).start()
        while True:
            frame = frames.get()
            if frame is sentinel:
                break
            yield frame

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/tools")
def tools(request: Request) -> dict:
    registry = request.app.state.tools_registry
    items = [{"name": registry[k].name, "description": registry[k].description}
             for k in sorted(registry)]
    return _ok(items)


@router.get("/knowledge")
def knowledge(request: Request, query: str | None = None, app: str | None = None,
              limit: int = 10) -> dict:
    service: KnowledgeService = request.app.state.knowledge
    items: list[KnowledgeItem] = service.search(query, app, limit)
    return _ok([i.to_camel_json() for i in items])


@router.delete("/knowledge/{item_id}")
def delete_knowledge(request: Request, item_id: int) -> dict:
    service: KnowledgeService = request.app.state.knowledge
    return _ok(service.delete(item_id))


@router.delete("/session/{session_id}")
def clear_session(request: Request, session_id: str) -> dict:
    request.app.state.ai_service.clear(session_id)
    return _ok("cleared")
