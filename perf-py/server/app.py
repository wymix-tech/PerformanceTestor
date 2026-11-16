"""FastAPI 应用工厂：阶段 4 server 骨架（AI 域先行）+ 阶段 5 按域接管（stress 域）。

依赖注入走 app.state（与 Java @Configuration Bean 装配等价）：
- ai_service: AiAgentService（会话/ReAct/沉淀）
- tools_registry: Agent 工具注册表（/api/ai/tools 与 Function Calling 同源）
- knowledge: KnowledgeService（SQLite 起步，RAG/MySQL 实现同契约可替换）
- stress_repo: StressRepository（SQLAlchemy，读路径已接管；写路径暂由 Java 承担）

LlmClient 由调用方注入（测试注入脚本化假客户端；生产 client_from_env()）。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from agents.llm import LlmClient
from server.ai_api import router as ai_router
from server.ai_service import AiAgentService
from server.db import database_url, make_session_factory
from server.errors import handle_exception, handle_validation
from server.knowledge import KnowledgeService, SqliteKnowledgeStore
from server.stress_api import router as stress_router
from server.stress_repo import StressRepository
from tools.registry import build_registry


def create_app(llm: LlmClient | None = None, knowledge: KnowledgeService | None = None,
               registry=None, include_knowledge_tool: bool = True,
               stress_repo: StressRepository | None = None,
               db_url: str | None = None) -> FastAPI:
    """构建 FastAPI 应用。

    include_knowledge_tool：把 knowledge_search 挂入工具注册表（AI 可检索知识库）；
    registry 由外部注入时需自行保证包含所需工具。
    stress_repo：注入既有的 StressRepository（契约测试用种子库）；
    否则按 db_url/环境变量建库（默认 sqlite 内存库）。
    """
    if registry is None:
        registry = build_registry()
        if include_knowledge_tool and knowledge is not None:
            from tools.knowledge_tools import build_knowledge_tool

            registry["knowledge_search"] = build_knowledge_tool(knowledge)

    app = FastAPI(title="PerformanceTestor Python Server",
                  description="perf-py server 骨架（阶段 4 AI 域 + 阶段 5 按域绞杀）",
                  version="0.2.0")
    app.state.tools_registry = registry
    app.state.knowledge = knowledge if knowledge is not None \
        else KnowledgeService(SqliteKnowledgeStore())
    app.state.ai_service = AiAgentService(llm, registry, app.state.knowledge)
    app.state.stress_repo = stress_repo if stress_repo is not None \
        else StressRepository(make_session_factory(db_url or database_url()))
    app.include_router(ai_router)
    app.include_router(stress_router)
    # 全局异常处理对齐 Java GlobalExceptionHandler：任何异常 → 200 + code=1 错误包装
    app.add_exception_handler(RequestValidationError, handle_validation)
    app.add_exception_handler(Exception, handle_exception)
    return app
