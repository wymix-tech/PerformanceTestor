"""知识库服务：分析结论沉淀与检索（对齐 Java AiKnowledgeService 语义）。

存储抽象（预留 RAG 接口位）：KnowledgeStore 为检索/存取协议，当前提供
SQLite 起步实现（LIKE + 命中计数评分）；阶段 5 server 域迁移时可替换为
MySQL FULLTEXT（ngram）实现或向量检索实现，上层 KnowledgeService 语义不变。

语义逐项对齐 Java 侧：
- 沉淀：正文 < MIN_CONTENT_CHARS 不入库；与最近 DEDUP_LOOKBACK 条同应用结论
  bigram Jaccard ≥ DEDUP_SIMILARITY 判重跳过（检索结果不被重复项淹没）
- 检索：空白 query → 最近结论（这是"列最近"而非"检索无命中"）；
  无命中如实返回空列表，不做 recent 回落（避免污染模型判读）；
  检索异常才降级到最近结论列表
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass

MIN_CONTENT_CHARS = 40
MAX_LIMIT = 20
SNIPPET_CHARS = 400
DEDUP_LOOKBACK = 50
DEDUP_SIMILARITY = 0.9
DEDUP_SAMPLE_CHARS = 1000


@dataclass
class KnowledgeItem:
    """对齐 Java AiKnowledge 实体（Jackson 序列化为 camelCase）。"""

    id: int | None = None
    app_name: str = ""
    title: str = ""
    content: str = ""
    tags: str = ""
    source: str = ""
    create_time: str = ""
    score: float = 0.0

    def to_camel_json(self) -> dict:
        return {
            "id": self.id,
            "appName": self.app_name,
            "title": self.title,
            "content": self.content,
            "tags": self.tags,
            "source": self.source,
            "createTime": self.create_time,
            "score": self.score,
        }


class KnowledgeStore(ABC):
    """存储协议（RAG 预留位：未来向量检索实现同一契约，替换即可）。"""

    @abstractmethod
    def save(self, item: KnowledgeItem) -> int:
        """入库，返回 id。"""

    @abstractmethod
    def recent(self, app_name: str, limit: int) -> list[KnowledgeItem]:
        """最近结论（app_name 非空时含全局 appName='' 的结论）。"""

    @abstractmethod
    def search(self, query: str, app_name: str, limit: int) -> list[KnowledgeItem]:
        """关键词检索（含相关度评分；实现抛异常由上层降级）。"""

    @abstractmethod
    def delete(self, item_id: int) -> bool:
        """删除单条，返回是否存在。"""

    def delete_by_app(self, app_name: str) -> int:  # noqa: D401 清理钩子（阶段 5 步骤 9）
        """删除指定应用的结论（默认实现无存储，返回 0；供 cleanup-data 级联调用）。"""
        return 0

    def delete_global(self) -> int:
        """删除全局结论（app_name=''，高风险操作，仅在二次确认后调用）。"""
        return 0


class SqliteKnowledgeStore(KnowledgeStore):
    """SQLite 起步实现（单文件、零部署依赖）。

    检索策略：LIKE 命中（title/content/tags）+ 关键词出现次数评分。
    SQLite FTS5 unicode61 对中文不切词，起步阶段不以 FTS5 为准；
    MySQL 部署在阶段 5 换 FULLTEXT ngram（同一 KnowledgeStore 契约）。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_knowledge (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                app_name    TEXT    NOT NULL DEFAULT '',
                title       TEXT    NOT NULL DEFAULT '',
                content     TEXT    NOT NULL,
                tags        TEXT    NOT NULL DEFAULT '',
                source      TEXT    NOT NULL DEFAULT '',
                create_time TEXT    NOT NULL DEFAULT ''
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_knowledge_app "
                    "ON ai_knowledge(app_name, id DESC)")
        self.conn.commit()

    def save(self, item: KnowledgeItem) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO ai_knowledge (app_name, title, content, tags, source, create_time) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item.app_name, item.title, item.content, item.tags, item.source,
             item.create_time),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent(self, app_name: str, limit: int) -> list[KnowledgeItem]:
        cur = self.conn.cursor()
        if app_name:
            cur.execute(
                "SELECT id, app_name, title, content, tags, source, create_time "
                "FROM ai_knowledge WHERE app_name = ? OR app_name = '' "
                "ORDER BY id DESC LIMIT ?",
                (app_name, limit),
            )
        else:
            cur.execute(
                "SELECT id, app_name, title, content, tags, source, create_time "
                "FROM ai_knowledge ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return [self._row_to_item(r, 0.0) for r in cur.fetchall()]

    def search(self, query: str, app_name: str, limit: int) -> list[KnowledgeItem]:
        cur = self.conn.cursor()
        if app_name:
            cur.execute(
                "SELECT id, app_name, title, content, tags, source, create_time "
                "FROM ai_knowledge WHERE app_name = ? OR app_name = '' "
                "ORDER BY id DESC LIMIT ?",
                (app_name, DEDUP_LOOKBACK * 4),
            )
        else:
            cur.execute(
                "SELECT id, app_name, title, content, tags, source, create_time "
                "FROM ai_knowledge ORDER BY id DESC LIMIT ?",
                (DEDUP_LOOKBACK * 4,),
            )
        scored: list[KnowledgeItem] = []
        q = query.lower()
        for row in cur.fetchall():
            item = self._row_to_item(row, 0.0)
            haystack = f"{item.title} {item.content} {item.tags}".lower()
            if q not in haystack:
                continue
            item.score = float(haystack.count(q))
            scored.append(item)
        scored.sort(key=lambda i: i.score, reverse=True)
        return scored[:limit]

    def delete(self, item_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM ai_knowledge WHERE id = ?", (item_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_by_app(self, app_name: str) -> int:
        """对齐 DataCleanupMapper.deleteAiKnowledgeByApp（app_name<>'' 条件随语义保留）。"""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM ai_knowledge WHERE app_name = ? AND app_name <> ''", (app_name,))
        self.conn.commit()
        return cur.rowcount

    def delete_global(self) -> int:
        """对齐 deleteGlobalAiKnowledge：仅 includeGlobalKnowledge=true 且二次确认后调用。"""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM ai_knowledge WHERE app_name = ''")
        self.conn.commit()
        return cur.rowcount

    @staticmethod
    def _row_to_item(row, score: float) -> KnowledgeItem:
        return KnowledgeItem(id=row[0], app_name=row[1], title=row[2], content=row[3],
                             tags=row[4], source=row[5], create_time=row[6], score=score)


class KnowledgeService:
    """沉淀/检索/删除（业务语义层，对齐 Java AiKnowledgeService）。"""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def save(self, app_name: str | None, title: str | None,
             content: str | None, tags: str | None) -> bool:
        if content is None or len(content.strip()) < MIN_CONTENT_CHARS:
            return False
        try:
            normalized = content.strip()
            if self._is_duplicate(app_name or "", normalized):
                return False
            from datetime import datetime

            self.store.save(KnowledgeItem(
                app_name=app_name or "",
                title=_truncate(title.strip() if title and title.strip()
                                else _first_line(normalized), 120),
                content=normalized,
                tags=tags or "",
                source="ai",
                create_time=datetime.now().isoformat(timespec="seconds"),
            ))
            return True
        except Exception:  # noqa: BLE001 沉淀失败不阻断对话链路（对齐 Java）
            return False

    def _is_duplicate(self, app_name: str, content: str) -> bool:
        try:
            recent = self.store.recent(app_name, DEDUP_LOOKBACK)
        except Exception:  # noqa: BLE001 比对失败宁可写入重复也不阻塞
            return False
        for item in recent:
            if not item.content:
                continue
            if similarity(content, item.content) >= DEDUP_SIMILARITY:
                return True
        return False

    def search(self, query: str | None, app_name: str | None, limit: int) -> list[KnowledgeItem]:
        bounded = max(1, min(limit if limit > 0 else 5, MAX_LIMIT))
        try:
            if query is None or not query.strip():
                return self.store.recent(app_name or "", bounded)
            hits = self.store.search(query.strip(), app_name or "", bounded)
            return hits or []  # 无命中如实返回空，不做 recent 回落
        except Exception:  # noqa: BLE001 检索异常才降级（FULLTEXT 不可用等）
            try:
                return self.store.recent(app_name or "", bounded)
            except Exception:  # noqa: BLE001
                return []

    def delete(self, item_id: int) -> bool:
        try:
            return self.store.delete(item_id)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def snippet(content: str) -> str:
        return _truncate(content, SNIPPET_CHARS)


def similarity(a: str, b: str) -> float:
    """字符 bigram Jaccard（逐行对齐 Java AiKnowledgeService.similarity）。"""
    ga = _bigrams(a)
    gb = _bigrams(b)
    if not ga or not gb:
        return 0.0
    intersection = len(ga & gb)
    return intersection / (len(ga) + len(gb) - intersection)


def _bigrams(text: str) -> set[str]:
    import re

    s = re.sub(r"\s+", "", text)
    if len(s) > DEDUP_SAMPLE_CHARS:
        s = s[:DEDUP_SAMPLE_CHARS]
    grams = {s[i:i + 2] for i in range(len(s) - 1)}
    if not grams and s:
        grams.add(s)
    return grams


def _first_line(content: str) -> str:
    nl = content.find("\n")
    line = content[:nl] if nl > 0 else content
    return line.strip()


def _truncate(text: str | None, max_chars: int) -> str:
    if text is None:
        return ""
    return text if len(text) <= max_chars else text[:max_chars]
