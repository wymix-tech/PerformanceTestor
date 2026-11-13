"""Python server 骨架（阶段 4）：AI 域先行接管 /api/ai，绞杀者模式按域迁移。"""

from __future__ import annotations

from server.ai_service import AiAgentService, detect_role
from server.app import create_app
from server.knowledge import (
    KnowledgeItem,
    KnowledgeService,
    KnowledgeStore,
    SqliteKnowledgeStore,
    similarity,
)

__all__ = [
    "AiAgentService",
    "detect_role",
    "create_app",
    "KnowledgeItem",
    "KnowledgeService",
    "KnowledgeStore",
    "SqliteKnowledgeStore",
    "similarity",
]
