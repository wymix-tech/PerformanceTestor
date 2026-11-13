"""知识库检索工具：AI 会话内检索沉淀的历史分析结论（对齐 Java SearchKnowledgeTool）。

与 server.knowledge.KnowledgeService 共享同一实例（由 app.py 工厂注入），
独立使用 perf tools call 时亦可传入自定义 store。
"""

from __future__ import annotations

from server.knowledge import KnowledgeService
from tools.registry import Tool

KNOWLEDGE_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "检索词（中文即可；空则返回最近结论）"},
        "app": {"type": "string", "description": "应用名过滤（空表示不过滤）"},
        "limit": {"type": "integer", "description": "条数上限（默认 5，最大 20）",
                  "default": 5},
    },
    "required": [],
}


def build_knowledge_tool(service: KnowledgeService) -> Tool:
    """以注入的 KnowledgeService 构建工具实例（会话与工具共享同一知识库）。"""

    def knowledge_search(args: dict) -> dict:
        query = (args.get("query") or "").strip()
        app = args.get("app") or None
        limit = int(args.get("limit") or 5)
        items = service.search(query, app, limit)
        if not items:
            return {"hits": [], "note": "暂无历史经验" if query else "知识库为空"}
        return {
            "hits": [{
                "id": i.id,
                "appName": i.app_name,
                "title": i.title,
                "snippet": service.snippet(i.content),
                "tags": i.tags,
                "score": i.score,
            } for i in items],
        }

    return Tool(
        name="knowledge_search",
        description="检索平台沉淀的历史性能分析结论与修复经验（中文全文检索），"
                    "供归因时参考同应用的历史经验；无命中时如实返回空。",
        parameters=KNOWLEDGE_SEARCH_SCHEMA,
        handler=knowledge_search,
    )
