"""FastAPI 应用工厂：阶段 4 AI 域 + 阶段 5 按域绞杀（stress/collector/datagen/auth/source）。

依赖注入走 app.state（与 Java @Configuration Bean 装配等价）：
- auth_props / jwt_service：单管理员 JWT 鉴权（perf.auth.* 语义）；
- ai_service / tools_registry / knowledge：AI 域（阶段 4）；
- stress_repo / collector_repo / datagen_repo / source_repo：SQLAlchemy 各域仓储。

鉴权（对齐 Java WebAuthConfig）：
- JWT 拦截 /api/**（白名单 /api/auth/**、/api/agent/**），X-Perf-Api-Token 并行放行；
- 探针通道独立令牌 X-Perf-Agent-Token（令牌为空时保持开放）；
- 鉴权开关 PERF_AUTH_ENABLED（Java perf.auth.enabled，默认 true）。

LlmClient 由调用方注入（测试注入脚本化假客户端；生产 client_from_env()）。
"""

from __future__ import annotations

import hmac

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agents.llm import LlmClient
from server.ai_api import router as ai_router
from server.ai_service import AiAgentService
from server.auth_api import router as auth_router
from server.auth_service import AUTH_FAIL_BODY, AuthProperties, JwtService
from server.collector_api import router as collector_router
from server.collector_repo import CollectorRepository
from server.datagen_api import router as datagen_router
from server.datagen_repo import DatagenRepository
from server.db import database_url, make_session_factory
from server.errors import handle_exception, handle_validation
from server.knowledge import KnowledgeService, SqliteKnowledgeStore
from server.source_api import router as source_router
from server.source_repo import SourceRepository
from server.stress_api import router as stress_router
from server.stress_repo import StressRepository
from tools.registry import build_registry


def _jwt_authorized(request: Request, props: AuthProperties,
                    jwt_service: JwtService) -> bool:
    """对齐 JwtAuthInterceptor.preHandle：CI 静态令牌先行，JWT Bearer 其次。"""
    if props.api_token:
        provided = request.headers.get("X-Perf-Api-Token")
        if provided is not None and hmac.compare_digest(
                props.api_token.encode("utf-8"), provided.encode("utf-8")):
            request.state.ci = True
            return True
    auth = request.headers.get("Authorization")
    if auth is not None and auth.startswith("Bearer "):
        subject = jwt_service.verify(auth[7:])
        if subject is not None:
            request.state.user = subject
            return True
    return False


def create_app(llm: LlmClient | None = None, knowledge: KnowledgeService | None = None,
               registry=None, include_knowledge_tool: bool = True,
               stress_repo: StressRepository | None = None,
               collector_repo: CollectorRepository | None = None,
               datagen_repo: DatagenRepository | None = None,
               source_repo: SourceRepository | None = None,
               db_url: str | None = None,
               auth: AuthProperties | None = None,
               auth_enabled: bool | None = None) -> FastAPI:
    """构建 FastAPI 应用。

    include_knowledge_tool：把 knowledge_search 挂入工具注册表（AI 可检索知识库）。
    各 repo：注入既有仓储（契约测试用种子库）；否则按 db_url/环境变量建库。
    auth：注入完整鉴权配置；或仅以 auth_enabled 覆盖开关（None = 读
    PERF_AUTH_ENABLED，Java perf.auth.enabled 默认 true）。
    """
    if registry is None:
        registry = build_registry()
        if include_knowledge_tool and knowledge is not None:
            from tools.knowledge_tools import build_knowledge_tool

            registry["knowledge_search"] = build_knowledge_tool(knowledge)

    app = FastAPI(title="PerformanceTestor Python Server",
                  description="perf-py server 骨架（阶段 4 AI 域 + 阶段 5 按域绞杀）",
                  version="0.3.0")
    app.state.tools_registry = registry
    app.state.knowledge = knowledge if knowledge is not None \
        else KnowledgeService(SqliteKnowledgeStore())
    app.state.ai_service = AiAgentService(llm, registry, app.state.knowledge)
    session_factory = make_session_factory(db_url or database_url())
    app.state.stress_repo = stress_repo if stress_repo is not None \
        else StressRepository(session_factory)
    app.state.collector_repo = collector_repo if collector_repo is not None \
        else CollectorRepository(session_factory)
    app.state.datagen_repo = datagen_repo if datagen_repo is not None \
        else DatagenRepository(session_factory)
    app.state.source_repo = source_repo if source_repo is not None \
        else SourceRepository(session_factory)
    app.state.auth_props = auth if auth is not None \
        else AuthProperties(enabled=auth_enabled)
    app.state.jwt_service = JwtService(app.state.auth_props)
    # collector_api 沿用此二态（探针令牌独立校验，enabled 与 JWT 开关同源）
    app.state.agent_token = app.state.auth_props.agent_token
    app.state.agent_auth_enabled = app.state.auth_props.enabled

    @app.middleware("http")
    async def jwt_guard(request: Request, call_next):
        """对齐 JwtAuthInterceptor：/api/** 拦截，auth/agent 白名单，401 固定体。"""
        props: AuthProperties = app.state.auth_props
        path = request.url.path
        if props.enabled and path.startswith("/api/") \
                and not path.startswith("/api/auth/") \
                and not path.startswith("/api/agent/") \
                and not _jwt_authorized(request, props, app.state.jwt_service):
            return JSONResponse(status_code=401, content=AUTH_FAIL_BODY)
        return await call_next(request)

    app.include_router(auth_router)
    app.include_router(ai_router)
    app.include_router(stress_router)
    app.include_router(collector_router)
    app.include_router(datagen_router)
    app.include_router(source_router)
    # 全局异常处理对齐 Java GlobalExceptionHandler：任何异常 → 200 + code=1 错误包装
    app.add_exception_handler(RequestValidationError, handle_validation)
    app.add_exception_handler(Exception, handle_exception)
    return app
